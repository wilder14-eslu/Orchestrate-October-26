"""Request-scoped linking of resources to the deletion subject.

Two layers:
1. Evidence links. An observation links its resource to request r when one of its
   identifiers (email, phone, hash, user_id) belongs to r's subject profile and the
   observation time lies in r's window (previous request's audit_at of the same person,
   r.audit_at]. Files captured after audit_at may link only through lines timestamped at
   or before audit_at (they are never used for state or citations; see state.py).
2. Worker attribution. Deleted accounts and anonymized records keep no identifier, so
   deletion_worker activity is attributed to requests:
   - account events: anchored when the account is evidence-linked; otherwise candidates
     inside [reported_at-90m, reported_at+15m] are scored with distributions learned at
     runtime from anchored requests only (offset to reported_at, result vs reported_status,
     sub-minute signature of the request's own linked worker events);
   - run events: other worker events within the learned run span after an attributed
     account event, matched by their sub-minute signature (per message template).
   --attribution strict keeps only unique candidates after hard filters and elimination;
   --attribution scored assigns by score and leaves a request unlinked only on an exact tie.
OCR-suspect observations link only when another independent source mentions the same id.
"""
from __future__ import annotations

import math
import re
from functools import lru_cache
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .identity import IdentityIndex, IdentityProfile
from .models import Activity, Corpus, Identifiers, Observation, Request

WINDOW_BEFORE = timedelta(minutes=90)   # H1 hard window around reported_at
WINDOW_AFTER = timedelta(minutes=15)
RUN_SPAN_FLOOR = timedelta(hours=4)     # maximum run length if anchored data is too thin
SUBMINUTE_TOLERANCE = 1                 # seconds; neighbouring values are rounding of the same offset
LAPLACE = 1.0


@lru_cache(maxsize=None)
def ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Link:
    case_id: str
    system_id: str
    resource_id: str
    via: str                 # KEY_MATCH, USER_ID, CATALOG, WORKER_ACCOUNT, WORKER_RUN
    evidence_id: str
    observed_at: str
    confidence: float
    detail: str = ""
    margin: Optional[float] = None   # scored attribution: best score minus runner-up (None for evidence links)


@dataclass
class LinkResult:
    links: Dict[str, List[Link]] = field(default_factory=lambda: defaultdict(list))
    windows: Dict[str, Tuple[Optional[str], str]] = field(default_factory=dict)
    profiles: Dict[str, IdentityProfile] = field(default_factory=dict)
    notes: Dict[str, List[str]] = field(default_factory=lambda: defaultdict(list))
    model: Dict[str, object] = field(default_factory=dict)

    def resources(self, case_id: str) -> Set[Tuple[str, str]]:
        return {(l.system_id, l.resource_id) for l in self.links.get(case_id, [])}


def _matches(obs_ids: Identifiers, prof: Identifiers) -> Optional[str]:
    if obs_ids.emails & prof.emails:
        return "KEY_MATCH"
    if obs_ids.hashes & prof.hashes:
        return "KEY_MATCH"
    if obs_ids.phones & prof.phones:
        return "KEY_MATCH"
    if obs_ids.user_ids & prof.user_ids:
        return "USER_ID"
    return None


def template(a: Activity) -> str:
    """Signature key of a worker event: event type + message template (resource ids removed)."""
    return a.event_type + "|" + re.sub(r"\b[a-z]+(?:_[a-z]+)*_\d+\b", "X", a.details).strip()


FAMILY_PATTERNS = (("POLICY", re.compile(r"^Policy action completed", re.I)),
                   ("FINAL", re.compile(r"received the final result", re.I)),
                   ("LEGACY", re.compile(r"reported the requested operation|stopped after the target system", re.I)))
FAMILY_MISMATCH_PENALTY = math.log(60)  # cancels the best possible signature evidence


def family(a: Activity) -> Optional[str]:
    """Message family of a worker run event; checks and unknown messages are neutral (None)."""
    for name, pattern in FAMILY_PATTERNS:
        if pattern.search(a.details):
            return name
    return None


def is_account_event(a: Activity) -> bool:
    return a.source_system == "deletion_worker" and a.target_system == "account_service" and a.event_type == "DELETE_RECORD"


EXPECTED_RESULT = {"COMPLETE": "SUCCESS", "NOT_STARTED": "FAILED", "PARTIALLY_COMPLETE": "PARTIAL"}


class Linker:
    def __init__(self, corpus: Corpus, index: IdentityIndex, requests: Iterable[Request], attribution: str = "scored",
                 run_min_margin: float = 2.0):
        # Scored attribution thresholds, split by type (architect decision):
        # - accounts: no margin threshold (an unlinked account is a certain miss); only exact ties stay unlinked;
        # - run events (analytics and other records): a margin below run_min_margin leaves the event unlinked,
        #   because a wrong low-margin attribution adds a false step to a case that may have none.
        self.run_min_margin = run_min_margin
        self.corpus = corpus
        self.index = index
        self.requests = sorted(requests, key=lambda r: (r.audit_at, r.case_id))
        self.attribution = attribution
        self.obs_by_resource: Dict[str, List[Observation]] = defaultdict(list)
        for o in corpus.observations:
            self.obs_by_resource[o.resource_id].append(o)
        self.worker = sorted((a for a in corpus.activity if a.source_system == "deletion_worker"),
                             key=lambda a: (a.event_time, a.activity_id))
        self.account_events = [a for a in self.worker if is_account_event(a)]

    # ------------------------------------------------------------------ evidence links
    def _windows(self, result: LinkResult) -> None:
        by_user: Dict[str, List[Request]] = defaultdict(list)
        for r in self.requests:
            res = self.index.resolve(r)
            if res.user_id:
                by_user[res.user_id].append(r)
                result.profiles[r.case_id] = self.index.profile(res.user_id, r.audit_at)
            else:
                result.notes[r.case_id].append(f"identity {res.via}: {res.reason}")
        for user, reqs in by_user.items():
            reqs.sort(key=lambda r: r.audit_at)
            for i, r in enumerate(reqs):
                result.windows[r.case_id] = (reqs[i - 1].audit_at if i else None, r.audit_at)

    def _corroborated(self, obs: Observation) -> bool:
        return any(o.evidence_id != obs.evidence_id and o.attrs.get("ocr_suspect") != "true"
                   for o in self.obs_by_resource[obs.resource_id])

    def _identifier_index(self) -> Dict[Tuple[str, str], List[Observation]]:
        index: Dict[Tuple[str, str], List[Observation]] = defaultdict(list)
        for o in self.corpus.observations:
            for kind, values in (("e", o.identifiers.emails), ("h", o.identifiers.hashes), ("p", o.identifiers.phones),
                                 ("u", o.identifiers.user_ids)):
                for v in values:
                    index[(kind, v)].append(o)
        return index

    def _evidence_links(self, result: LinkResult) -> None:
        by_identifier = self._identifier_index()
        for r in self.requests:
            prof = result.profiles.get(r.case_id)
            if not prof:
                continue
            ids = prof.identifiers()
            start, end = result.windows[r.case_id]
            candidates = {id(o): o for kind, values in (("e", ids.emails), ("h", ids.hashes), ("p", ids.phones), ("u", ids.user_ids))
                          for v in values for o in by_identifier.get((kind, v), [])}
            for obs in sorted(candidates.values(), key=lambda o: o.obs_id):
                when = obs.observed_at
                if when > end or (start is not None and when <= start):
                    continue
                if obs.is_claim and obs.attrs.get("event_type") != "LINK_RESOURCE_IDENTITY":
                    continue
                via = _matches(obs.identifiers, ids)
                if not via:
                    continue
                if obs.source_kind == "ACTIVITY":
                    via = "CATALOG"
                if obs.attrs.get("ocr_suspect") == "true" and not self._corroborated(obs):
                    result.notes[r.case_id].append(f"uncorroborated OCR suspect {obs.resource_id} in {obs.evidence_id} ignored")
                    continue
                result.links[r.case_id].append(Link(r.case_id, obs.system_id, obs.resource_id, via, obs.evidence_id,
                                                    obs.observed_at, obs.confidence))

    # ------------------------------------------------------------------ learned attribution model
    def _learn(self, result: LinkResult, anchored: Dict[str, Activity]) -> None:
        offsets, matches, sub = [], Counter(), defaultdict(Counter)
        spans = []
        by_case = {r.case_id: r for r in self.requests}
        for cid, a in anchored.items():
            r = by_case[cid]
            offsets.append((ts(a.event_time) - ts(r.reported_at)).total_seconds() / 60)
            matches[EXPECTED_RESULT.get(r.reported_status) == a.reported_result] += 1
            linked = {res for _, res in result.resources(cid)} - {a.resource_id}
            for b in self.worker:
                if b.resource_id in linked and not is_account_event(b):
                    d = (ts(b.event_time) - ts(a.event_time)).total_seconds()
                    if 0 < d <= RUN_SPAN_FLOOR.total_seconds() * 2:
                        m = int(d) % 60
                        sub[template(b)][m] += 1
                        sub[template(b).split("|", 1)[1]][m] += 1   # backoff: template only
                        sub["*"][m] += 1
                        spans.append(d)
        mu = sum(offsets) / len(offsets) if offsets else -60.0
        sd = max(3.0, math.sqrt(sum((x - mu) ** 2 for x in offsets) / max(1, len(offsets) - 1))) if offsets else 10.0
        span = timedelta(seconds=max(spans) * 1.1) if spans else RUN_SPAN_FLOOR
        result.model = {"anchored": len(anchored), "offset_mu": mu, "offset_sd": sd,
                        "p_result_match": (matches[True] + 1) / (sum(matches.values()) + 2),
                        "sub_minute": {k: dict(v) for k, v in sub.items()}, "run_span_s": span.total_seconds()}
        self._sub = sub
        self._span = span

    def _table(self, key: str) -> Counter:
        for k in (key, key.split("|", 1)[-1], "*"):
            if sum(self._sub.get(k, Counter()).values()) >= 5:
                return self._sub[k]
        return self._sub.get("*", Counter())

    def _p_sub(self, key: str, m: int) -> float:
        table = self._table(key)
        total = sum(table.values())
        hits = sum(table.get((m + d) % 60, 0) for d in range(-SUBMINUTE_TOLERANCE, SUBMINUTE_TOLERANCE + 1))
        return (hits + LAPLACE) / (total + 60 * LAPLACE)

    def _sub_ok(self, key: str, m: int) -> bool:
        table = self._table(key)
        return any(table.get((m + d) % 60, 0) > 0 for d in range(-SUBMINUTE_TOLERANCE, SUBMINUTE_TOLERANCE + 1))

    # ------------------------------------------------------------------ account attribution
    def _account_candidates(self, r: Request) -> List[Activity]:
        lo, hi = ts(r.reported_at) - WINDOW_BEFORE, ts(r.reported_at) + WINDOW_AFTER
        return [a for a in self.account_events if lo <= ts(a.event_time) <= hi]

    def _own_run_events(self, result: LinkResult, r: Request, account: Activity) -> List[Activity]:
        linked = {res for _, res in result.resources(r.case_id)}
        t0 = ts(account.event_time)
        return [b for b in self.worker if b.resource_id in linked and not is_account_event(b)
                and timedelta(0) < ts(b.event_time) - t0 <= self._span]

    def _score(self, result: LinkResult, r: Request, a: Activity) -> float:
        m = result.model
        off = (ts(a.event_time) - ts(r.reported_at)).total_seconds() / 60
        s = -0.5 * ((off - m["offset_mu"]) / m["offset_sd"]) ** 2
        p = m["p_result_match"]
        s += math.log(p) if EXPECTED_RESULT.get(r.reported_status) == a.reported_result else math.log(1 - p)
        for b in self._own_run_events(result, r, a):  # log-likelihood ratio against a uniform second
            s += math.log(60 * self._p_sub(template(b), int((ts(b.event_time) - ts(a.event_time)).total_seconds()) % 60))
        return s

    def _attribute_accounts(self, result: LinkResult) -> Dict[str, Activity]:
        account_of_case: Dict[str, Activity] = {}
        anchored_resources: Dict[str, str] = {}
        self._margins: Dict[str, float] = {}
        for r in self.requests:
            linked = {res for _, res in result.resources(r.case_id)}
            for a in self._account_candidates(r):
                if a.resource_id in linked:
                    account_of_case.setdefault(r.case_id, a)
                    anchored_resources[a.resource_id] = r.case_id
        self._learn(result, account_of_case)
        used = {a.activity_id for a in account_of_case.values()}
        open_cases = [r for r in self.requests if r.case_id not in account_of_case and r.case_id in result.profiles]
        cand: Dict[str, List[Activity]] = {}
        for r in open_cases:
            cand[r.case_id] = [a for a in self._account_candidates(r)
                               if a.activity_id not in used and a.resource_id not in anchored_resources]
        by_case = {r.case_id: r for r in self.requests}
        if self.attribution == "strict":
            for cid in cand:
                r = by_case[cid]
                cand[cid] = [a for a in cand[cid] if EXPECTED_RESULT.get(r.reported_status) == a.reported_result
                             and all(self._sub_ok(template(b), int((ts(b.event_time) - ts(a.event_time)).total_seconds()) % 60)
                                     for b in self._own_run_events(result, r, a))]
            changed = True
            while changed:
                changed = False
                for cid, lst in cand.items():
                    if cid in account_of_case:
                        continue
                    lst[:] = [a for a in lst if a.activity_id not in used]
                    if len(lst) == 1:
                        account_of_case[cid] = lst[0]; used.add(lst[0].activity_id); changed = True
            for cid, lst in cand.items():
                if cid not in account_of_case and len(lst) > 1:
                    result.notes[cid].append(f"account unlinked: {len(lst)} candidates remain (strict)")
        else:
            # Margin-first assignment: the request whose best candidate beats its runner-up by the
            # largest margin is assigned first; an exact tie between its top two leaves it unlinked.
            scores = {cid: {a.activity_id: (self._score(result, by_case[cid], a), a) for a in lst} for cid, lst in cand.items()}
            open_ids = {cid for cid, d in scores.items() if d}
            while open_ids:
                best = None
                for cid in sorted(open_ids):
                    free = sorted(((sc, aid, a) for aid, (sc, a) in scores[cid].items() if aid not in used),
                                  key=lambda x: (-x[0], x[1]))
                    if not free:
                        continue
                    margin = free[0][0] - free[1][0] if len(free) > 1 else float("inf")
                    key = (margin, free[0][0], cid)
                    if best is None or key[0] > best[0][0] or (key[0] == best[0][0] and (key[1], key[2]) > (best[0][1], best[0][2])):
                        best = (key, cid, free)
                if best is None:
                    break
                (margin, _, _), cid, free = best
                open_ids.discard(cid)
                if margin == 0:
                    result.notes[cid].append(f"account unlinked: exact score tie between {free[0][1]} and {free[1][1]}")
                    continue
                account_of_case[cid] = free[0][2]
                used.add(free[0][1])
                self._margins[cid] = margin
        for cid, a in account_of_case.items():
            if a.resource_id not in {res for _, res in result.resources(cid)}:
                result.links[cid].append(Link(cid, "account_service", a.resource_id, "WORKER_ACCOUNT", a.activity_id,
                                              a.event_time, 0.7, f"{self.attribution} attribution",
                                              self._margins.get(cid)))
        return account_of_case

    # ------------------------------------------------------------------ run attribution
    def _run_families(self, result: LinkResult, cid: str, account: Activity) -> Set[str]:
        """Families of the worker events of the request's evidence-linked resources in this run."""
        by_case = {r.case_id: r for r in self.requests}
        return {f for f in (family(b) for b in self._own_run_events(result, by_case[cid], account)) if f}

    def _attribute_runs(self, result: LinkResult, accounts: Dict[str, Activity]) -> None:
        """Attach other worker events to the run of an attributed account event by sub-minute signature,
        penalizing a message family that differs from the family of the request's own linked events."""
        evidence_linked = {l.resource_id for links in result.links.values() for l in links}
        by_case = {r.case_id: r for r in self.requests}
        families = {cid: self._run_families(result, cid, a) for cid, a in accounts.items()}
        for b in self.worker:
            if is_account_event(b) or b.resource_id in evidence_linked:
                continue
            tb = ts(b.event_time)
            fam = family(b)
            options = []
            for cid, a in accounts.items():
                r = by_case[cid]
                d = (tb - ts(a.event_time)).total_seconds()
                if 0 < d <= self._span.total_seconds() and b.event_time <= r.audit_at:
                    m = int(d) % 60
                    if not self._sub_ok(template(b), m):
                        continue
                    score = math.log(60 * self._p_sub(template(b), m))
                    mismatch = bool(fam and families[cid] and fam not in families[cid])
                    if mismatch and self.attribution == "strict":
                        continue
                    if mismatch:
                        score -= FAMILY_MISMATCH_PENALTY
                    options.append((score, cid))
            options = [o for o in options if o[0] > 0]  # signature must fit better than chance
            if not options:
                continue
            options.sort(key=lambda x: (-x[0], x[1]))
            if len(options) > 1 and (self.attribution == "strict" or options[0][0] == options[1][0]):
                continue
            cid = options[0][1]
            margin = options[0][0] - (options[1][0] if len(options) > 1 else 0.0)  # runner-up or chance level
            if self.attribution == "scored" and margin < self.run_min_margin:
                result.notes[cid].append(f"run event {b.resource_id} unlinked: margin {margin:.3f} below {self.run_min_margin}")
                continue
            result.links[cid].append(Link(cid, b.target_system, b.resource_id, "WORKER_RUN", b.activity_id, b.event_time, 0.6,
                                          f"{b.event_type} {b.reported_result} family={fam}", margin))

    def link(self) -> LinkResult:
        result = LinkResult()
        self._windows(result)
        self._evidence_links(result)
        accounts = self._attribute_accounts(result)
        self._attribute_runs(result, accounts)
        return result
