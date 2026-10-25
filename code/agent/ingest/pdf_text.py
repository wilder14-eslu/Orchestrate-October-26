"""PDF evidence -> text -> document records (stdlib only).

Text extraction order: the built-in extractor below (Flate/ASCII85 streams,
Tj/TJ operators, ToUnicode CMaps), which is enough for the ReportLab files in
the dataset. Documents are business artifacts, so records are built from the
resource ids, identifiers, references and retention dates the text contains.
"""
from __future__ import annotations

import base64
import re
import zlib
from collections import Counter
from typing import Dict, List, Optional, Tuple

from .. import config as C
from ..models import CaptureScope, ManifestEntry, Observation, ParseWarning
from .records import RecordContext, interpret

OBJ_RE = re.compile(rb"(\d+)\s+0\s+obj(.*?)endobj", re.S)
STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\n?endstream", re.S)
ALIAS_RE = re.compile(r"\b(?:ORD-\d{6,}|CS-\d{3,}|NST-\d{4}-\d{4}-\d{3,}|FIN-\d{3,}|CJX-\d{8}-\d{4})\b")
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DOCUMENT_CONFIDENCE = 0.85
SUSPECT_CONFIDENCE = 0.2


# ------------------------------------------------------------------ low-level PDF decoding
def _decode_stream(obj: bytes) -> Optional[bytes]:
    m = STREAM_RE.search(obj)
    if not m:
        return None
    data = m.group(1)
    head = obj[: m.start()]
    filters = re.findall(rb"/(ASCII85Decode|FlateDecode|A85|Fl)\b", head)
    try:
        for f in filters:
            if f in (b"ASCII85Decode", b"A85"):
                data = data.strip()
                if data.endswith(b"~>"):
                    data = data[:-2]
                data = base64.a85decode(data)
            elif f in (b"FlateDecode", b"Fl"):
                data = zlib.decompress(data)
    except (ValueError, zlib.error):
        return None
    return data


def _parse_cmap(data: bytes) -> Dict[int, str]:
    cmap: Dict[int, str] = {}
    text = data.decode("latin-1")
    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            cmap[int(src, 16)] = bytes.fromhex(dst).decode("utf-16-be", "replace")
    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        for lo, hi, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            base = int(dst, 16)
            for i, code in enumerate(range(int(lo, 16), int(hi, 16) + 1)):
                cmap[code] = chr(base + i)
    return cmap


def _literal(s: bytes, i: int) -> Tuple[bytes, int]:
    """Read a PDF literal string starting after '('; return (bytes, index after ')')."""
    out, depth = bytearray(), 1
    escapes = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}
    while i < len(s):
        c = s[i:i + 1]
        if c == b"\\":
            nxt = s[i + 1:i + 2]
            if nxt in escapes:
                out += escapes[nxt]; i += 2
            elif nxt.isdigit():
                m = re.match(rb"[0-7]{1,3}", s[i + 1:i + 4])
                out.append(int(m.group(0), 8) & 0xFF); i += 1 + len(m.group(0))
            else:
                out += nxt; i += 2
            continue
        if c == b"(":
            depth += 1
        elif c == b")":
            depth -= 1
            if depth == 0:
                return bytes(out), i + 1
        out += c
        i += 1
    return bytes(out), i


def _show(raw: bytes, cmap: Optional[Dict[int, str]]) -> str:
    if cmap:
        return "".join(cmap.get(b, "") for b in raw)
    return raw.decode("cp1252", "replace")


def _content_text(content: bytes, fonts: Dict[str, Dict[int, str]]) -> str:
    out: List[str] = []
    cmap: Optional[Dict[int, str]] = None
    i, n = 0, len(content)
    operands: List[object] = []
    while i < n:
        c = content[i:i + 1]
        if c in b" \t\r\n":
            i += 1
        elif c == b"(":
            raw, i = _literal(content, i + 1)
            operands.append(raw)
        elif c == b"<" and content[i:i + 2] != b"<<":
            j = content.index(b">", i)
            operands.append(bytes.fromhex(content[i + 1:j].decode("latin-1").strip() or ""))
            i = j + 1
        elif c == b"[":
            j, items = i + 1, []
            while j < n and content[j:j + 1] != b"]":
                if content[j:j + 1] == b"(":
                    raw, j = _literal(content, j + 1)
                    items.append(raw)
                else:
                    m = re.match(rb"-?\d+(?:\.\d+)?", content[j:])
                    if m and float(m.group(0)) < -180:
                        items.append(b" ")
                    j += len(m.group(0)) if m else 1
            operands.append(items)
            i = j + 1
        elif c == b"/":
            m = re.match(rb"/[^\s/\[\]()<>]+", content[i:])
            operands.append(m.group(0).decode("latin-1"))
            i += len(m.group(0))
        else:
            m = re.match(rb"[^\s/\[\]()<>]+", content[i:])
            token = m.group(0) if m else content[i:i + 1]
            i += len(token)
            op = token.decode("latin-1")
            if op == "Tf" and len(operands) >= 2 and isinstance(operands[-2], str):
                cmap = fonts.get(operands[-2].lstrip("/"))
            elif op in ("Tj", "'", '"') and operands and isinstance(operands[-1], bytes):
                if op != "Tj":
                    out.append("\n")
                out.append(_show(operands[-1], cmap))
            elif op == "TJ" and operands and isinstance(operands[-1], list):
                out.append("".join(_show(x, cmap) if x != b" " else " " for x in operands[-1]))
            elif op in ("Td", "TD", "T*", "Tm", "ET"):
                if out and not out[-1].endswith("\n"):
                    out.append("\n")
            if not re.fullmatch(r"-?\d+(?:\.\d+)?", op):
                operands = []
            else:
                operands.append(op)
    return "".join(out)


def extract_text(data: bytes) -> str:
    objects = {int(m.group(1)): m.group(2) for m in OBJ_RE.finditer(data)}
    cmaps: Dict[int, Dict[int, str]] = {}
    fonts: Dict[str, Dict[int, str]] = {}
    for num, body in objects.items():
        ref = re.search(rb"/ToUnicode\s+(\d+)\s+0\s+R", body)
        if ref and int(ref.group(1)) in objects:
            stream = _decode_stream(objects[int(ref.group(1))])
            if stream:
                cmaps[num] = _parse_cmap(stream)
    for body in objects.values():
        for name, ref in re.findall(rb"/([\w+.-]+)\s+(\d+)\s+0\s+R", body):
            if int(ref) in cmaps:
                fonts[name.decode("latin-1")] = cmaps[int(ref)]
    pages = []
    for body in objects.values():
        if re.search(rb"/Type\s*/Page\b", body):
            for ref in re.findall(rb"/Contents\s+(\d+)\s+0\s+R", body):
                stream = _decode_stream(objects.get(int(ref), b""))
                if stream:
                    pages.append(_content_text(stream, fonts))
    return "\n".join(pages)


# ------------------------------------------------------------------ text -> records
FLOW_OFF = re.compile(r"\b(paused|disabled|stopped|inactive|suspended)\b", re.I)
FLOW_ON = re.compile(r"\b(continuous|active|enabled|running|streaming)\b", re.I)
RESTORE_OFF = re.compile(r"\b(restore\s+blocked|blocked|locked|restore\s+denied|deny)\b", re.I)
RESTORE_ON = re.compile(r"\b(restorable|restore\s+allowed|eligible)\b", re.I)
FILE_ALIAS_RE = re.compile(r"\b[\w.-]+\.(?:pdf|zip|csv|json)\b")


def clean_ocr(text: str) -> str:
    """Undo common OCR artifacts inside identifiers (stray bars and spaces around '@')."""
    text = re.sub(r"\s*\|\s*@", "@", text)
    text = re.sub(r"(\w)\s+@\s*(\w)", r"\1@\2", text)
    # Heal comma inside email local parts (e.g. 'a,b@example.com' -> 'a.b@example.com')
    text = re.sub(r"(\b[A-Za-z0-9_%+-]+),([A-Za-z0-9_%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b)", r"\1.\2", text)
    # Heal space after dotted token inside email local parts (e.g. 'a.b c@example.com' -> 'a.b.c@example.com')
    text = re.sub(r"(\b[A-Za-z0-9_%+-]+\.[A-Za-z0-9_%+-]+)\s+([A-Za-z0-9_%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b", r"\1.\2", text)
    return text


def _edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def ocr_vote(text: str):
    """Clean OCR misreads across the union of OCR passes.

    Emails: a variant is removed when a near-identical email (edit distance 1) was read
    strictly more often; the majority spelling always survives, so nothing is lost.
    Resource ids: never removed (sibling rows differ by one digit, e.g. audit_record_001..006).
    An id read once while a one-edit sibling was read at least three times is returned as
    suspect, so it is kept at low confidence for the VLM layer to confirm.
    """
    emails = Counter(C.EMAIL_RE.findall(text))
    drop = set()
    for e in emails:
        e_local, _, e_dom = e.lower().partition("@")
        for o in emails:
            if o == e:
                continue
            o_local, _, o_dom = o.lower().partition("@")
            if o_dom == e_dom and (o_local.endswith("." + e_local) or o_local.endswith("-" + e_local)):
                drop.add(e)
                break
    for e, n in emails.items():
        if any(o != e and m > n and _edit_distance(e, o) <= 1 for o, m in emails.items()):
            drop.add(e)
    for tok in sorted(drop, key=len, reverse=True):
        text = re.sub(r"(?<![\w.@])" + re.escape(tok) + r"(?![\w@])", " ", text)
    ids = Counter(C.RESOURCE_RE.findall(text))
    suspects = {r for r, n in ids.items()
                if n == 1 and any(o != r and m >= 3 and _edit_distance(r, o) <= 1 for o, m in ids.items())}
    return text, suspects


def _dates(text: str) -> List[str]:
    out = []
    for d, mon, y in re.findall(r"retain(?:ed)?\s+until\s+(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{4})", text, re.I):
        out.append(f"{y}-{MONTHS.get(mon.lower(), 1):02d}-{int(d):02d}T23:59:59Z")
    return out


def document_records(entry: ManifestEntry, text: str, kind: str, method: str, confidence: float,
                     aliases: Optional[Dict[str, str]] = None):
    """Build one record per resource shown in a document or screenshot.

    Identifiers are taken from the lines that mention the resource. Only when the
    whole document concerns a single person (at most one email and one user_id)
    are document-wide identifiers attached to every resource, so table screens
    listing several customers never cross-link rows. Catalog aliases (business
    references such as NST-..., user_17 or a file name) map to resource ids.
    """
    text = clean_ocr(text)
    suspects: set = set()
    if method == "ocr":
        text, suspects = ocr_vote(text)
    lines = text.splitlines()
    aliases = aliases or {}
    doc_emails = {e.lower() for e in C.EMAIL_RE.findall(text)}
    doc_users = set(C.USER_RE.findall(text))
    single_subject = len(doc_emails) <= 1 and len(doc_users) <= 1 and bool(doc_emails or doc_users)

    # user_id aliases (a retained customer reference) only identify a resource in a single-subject document.
    tokens = set(ALIAS_RE.findall(text)) | set(FILE_ALIAS_RE.findall(text)) | (doc_users if single_subject else set())
    targets: Dict[str, List[str]] = {}
    for rid in sorted(set(C.RESOURCE_RE.findall(text))):
        targets[rid] = [l for l in lines if rid in C.RESOURCE_RE.findall(l)]
    for token in sorted(tokens):
        rid = aliases.get(token)
        if rid and rid not in targets:
            targets[rid] = [l for l in lines if token in l]
    retention = _dates(text)
    observations: List[Observation] = []
    for rid, rlines in targets.items():
        line_text = "\n".join(rlines)
        raw = {"resource_id": rid, "document_text": text if single_subject else line_text, "origin": entry.origin}
        system, category = C.system_and_category(rid)
        if category == "DATA_FLOW" and (FLOW_OFF.search(line_text) or FLOW_ON.search(line_text)):
            raw["enabled"] = not FLOW_OFF.search(line_text)
        if category == "BACKUP_ARCHIVE" and (RESTORE_OFF.search(line_text) or RESTORE_ON.search(line_text)):
            raw["restore_allowed"] = not RESTORE_OFF.search(line_text)
        if retention:
            raw["retention_until"] = retention[0]
        ctx = RecordContext(entry.file_id, kind, entry.captured_at, f"doc:{rid}", collection=entry.file_path,
                            extraction_method=method, confidence=SUSPECT_CONFIDENCE if rid in suspects else confidence,
                            extra={"aliases": ",".join(sorted(tokens & set(aliases)) or []), "single_subject": str(single_subject),
                                   **({"ocr_suspect": "true"} if rid in suspects else {})})
        obs = interpret(raw, ctx)
        if obs:
            observations.append(obs)
    unresolved = sorted(t for t in set(ALIAS_RE.findall(text)) if t not in aliases)
    mentions = [{"evidence_id": entry.file_id, "alias": a, "captured_at": entry.captured_at,
                 "text_ids": sorted(doc_emails | doc_users), "retention": retention} for a in unresolved]
    return observations, mentions


def parse(entry: ManifestEntry, data: bytes, warnings: List[ParseWarning], aliases: Optional[Dict[str, str]] = None):
    try:
        text = extract_text(data)
    except Exception as exc:  # malformed PDF structure: report and continue
        warnings.append(ParseWarning(entry.file_id, entry.file_path, "-", f"PDF text extraction failed: {exc}"))
        return [], [], []
    if not text.strip():
        warnings.append(ParseWarning(entry.file_id, entry.file_path, "-", "PDF contained no extractable text"))
        return [], [], []
    from . import injection
    injection.scan(entry, text, warnings)
    observations, mentions = document_records(entry, text, "PDF", "rule", DOCUMENT_CONFIDENCE, aliases)
    return observations, [], mentions
