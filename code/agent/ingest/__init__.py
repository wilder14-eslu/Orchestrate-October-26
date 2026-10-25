"""Ingestion: load every participant-facing file into one Corpus of observations.

Each evidence file is parsed by the parser for its extension. A failure in one
file is recorded as a ParseWarning and never stops the run. Activity rows are
kept as claims (is_claim=True); catalog links become identifier-bearing
observations without a state.
"""
from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List

from .. import config as C
from ..models import Activity, Corpus, Identifiers, Observation, ParseWarning
from . import image_text, injection, json_records, log_events, pdf_text, sql_snapshot, txt_records
from .csv_tables import load_tables
from .records import RecordContext, interpret, scan_identifiers

TEXT_PARSERS = {".sql": ("SQL", sql_snapshot.parse), ".json": ("JSON", json_records.parse),
                ".log": ("LOG", log_events.parse), ".txt": ("TXT", txt_records.parse)}


def activity_observation(a: Activity) -> Observation | None:
    system, category = C.system_and_category(a.resource_id)
    if not system:
        return None
    identifiers = scan_identifiers({"details": a.details}) if a.event_type == "LINK_RESOURCE_IDENTITY" else Identifiers()
    alias = ALIAS_IN_DETAILS.search(a.details) if a.event_type == "LINK_RESOURCE_IDENTITY" else None
    return Observation(
        obs_id=f"{a.activity_id}#row", evidence_id=a.activity_id, source_kind="ACTIVITY", file_captured_at=a.event_time,
        observed_at=a.event_time, system_id=a.target_system if a.target_system in {s for s, _ in C.RESOURCE_PREFIXES.values()} else system,
        resource_id=a.resource_id, replica_role="NONE", category=category, presence="UNKNOWN", observed_state=None,
        identifiers=identifiers, pii_fields=frozenset(), is_claim=True,
        attrs={"event_type": a.event_type, "reported_result": a.reported_result, "source_system": a.source_system,
               "target_system": a.target_system, "details": a.details[:200],
               **({"alias": (alias.group(2) or alias.group(4))} if alias else {})})


ALIAS_IN_DETAILS = re.compile(r"\blinked\s+(\S+)\s+\(([^)]+)\)|confirmed\s+(\S+)\s+\(([^)]+)\)")


def catalog_aliases(activity: List[Activity]) -> Dict[str, str]:
    """Business references the data catalog maps to resource ids, e.g. 'NST-2026-0718-1842' -> export_1842."""
    out: Dict[str, str] = {}
    for a in activity:
        if a.event_type != "LINK_RESOURCE_IDENTITY":
            continue
        m = ALIAS_IN_DETAILS.search(a.details)
        if m:
            alias = (m.group(2) or m.group(4)).strip()
            if alias != a.resource_id:
                out[alias] = a.resource_id
    return out


def known_identifiers(corpus: Corpus) -> Dict[str, set]:
    emails = {i.email.lower() for i in corpus.identities} | {a.old_value.lower() for a in corpus.audit_log if a.field_name == "email"}
    phones = {C.norm_phone(i.phone) for i in corpus.identities} | {C.norm_phone(a.old_value) for a in corpus.audit_log
                                                                     if a.field_name == "phone"}
    return {"emails": emails, "phones": phones, "user_ids": {i.user_id for i in corpus.identities}}


def load_corpus(data_dir: Path, ocr: bool = True, model_layer=None) -> Corpus:
    identities, audit_log, activity, rules, manifest = load_tables(data_dir)
    corpus = Corpus(identities, audit_log, activity, rules, manifest)
    aliases = catalog_aliases(activity)
    # Screenshots are parsed last so the VLM merge can check ids against every other source.
    manifest = sorted(manifest, key=lambda m: (Path(m.file_path).suffix.lower() in {".png", ".jpg", ".jpeg"}, m.file_id))
    manifest_by_suffix = Counter(Path(m.file_path).suffix.lower() for m in manifest)
    known_ids = known_identifiers(corpus)
    files = Counter()
    obs_by_kind = Counter()
    failed = Counter()
    crashes = Counter()
    for entry in manifest:
        path = data_dir / entry.file_path
        suffix = path.suffix.lower()
        before = len(corpus.warnings)
        try:
            if not path.exists():
                corpus.warnings.append(ParseWarning(entry.file_id, entry.file_path, "-", "file listed in manifest is missing"))
                failed[suffix] += 1
                continue
            if suffix in TEXT_PARSERS:
                kind, parser = TEXT_PARSERS[suffix]
                text = path.read_text(encoding="utf-8", errors="replace")
                injection.scan(entry, text, corpus.warnings)
                observations, scopes = parser(entry, text, corpus.warnings)
                mentions: List[dict] = []
            elif suffix == ".pdf":
                kind = "PDF"
                observations, scopes, mentions = pdf_text.parse(entry, path.read_bytes(), corpus.warnings, aliases)
            elif suffix in {".png", ".jpg", ".jpeg"}:
                kind = "IMAGE"
                known_resources = {o.resource_id for o in corpus.observations} | {a.resource_id for a in activity}
                observations, scopes, mentions = image_text.parse(entry, str(path), corpus.warnings, ocr, aliases=aliases,
                                                                  model_layer=model_layer, known_resources=known_resources,
                                                                  known_ids=known_ids)
            else:
                corpus.warnings.append(ParseWarning(entry.file_id, entry.file_path, "-", f"unsupported file type {suffix}"))
                failed[suffix] += 1
                continue
        except Exception as exc:  # defensive: one broken file must not stop the audit
            print(f"[ingest] parser crashed on {entry.file_id} ({entry.file_path}): {type(exc).__name__}: {exc}", file=sys.stderr)
            corpus.warnings.append(ParseWarning(entry.file_id, entry.file_path, "-", f"parser crashed: {type(exc).__name__}: {exc}"))
            failed[suffix] += 1
            crashes[suffix] += 1
            continue
        files[suffix] += 1
        obs_by_kind[suffix] += len(observations)
        if not observations and len(corpus.warnings) == before and suffix not in {".png"}:
            note = ("scope-only capture: no records, scope kept" if scopes else
                    f"no resource ids; {len(mentions)} business reference(s) kept" if mentions else "no observations extracted")
            corpus.warnings.append(ParseWarning(entry.file_id, entry.file_path, "-", note))
        corpus.observations.extend(observations)
        corpus.scopes.extend(scopes)
        corpus.alias_mentions.extend(mentions)
    claims = [o for o in (activity_observation(a) for a in activity) if o]
    corpus.observations.extend(claims)
    corpus.stats = {
        "files": dict(files),
        "manifest_files": dict(manifest_by_suffix),
        "observations": dict(obs_by_kind),
        "failed_files": dict(failed),
        "parser_crashes": dict(crashes),
        "activity_observations": {"rows": len(activity), "observations": len(claims)},
    }
    return corpus
