"""Deterministic detector for instruction-like text inside evidence (prompt-injection attempts).

Evidence is always data; this detector only records a warning so the trace shows the attempt.
It never changes extraction, state or decisions.
"""
from __future__ import annotations

import re
from typing import List

from ..models import ManifestEntry, ParseWarning

INJECTION_RE = re.compile(
    r"ignore\s+(?:all\s+|any\s+)?(?:the\s+)?(?:previous|prior|above|earlier)\s+(?:instructions|rules|prompts?)"
    r"|disregard\s+(?:all\s+|the\s+)?(?:previous|above|prior)"
    r"|(?:system|developer)\s+(?:instruction|prompt|notice|message)\s+(?:to|for)\s+(?:the\s+)?(?:ai|audit|assistant|model)"
    r"|(?:note|message|instruction)\s+to\s+(?:the\s+)?(?:ai|audit\s+agent|auditor|assistant|model)"
    r"|mark\s+(?:this|the)\s+(?:case|request|deletion)\s+(?:as\s+)?complete"
    r"|cite\s+no\s+evidence|you\s+are\s+(?:an?|the)\s+(?:ai|assistant|language\s+model)", re.I)


def scan(entry: ManifestEntry, text: str, warnings: List[ParseWarning]) -> None:
    hits = sorted({m.group(0).lower() for m in INJECTION_RE.finditer(text or "")})
    if hits:
        warnings.append(ParseWarning(entry.file_id, entry.file_path, "-",
                                     f"injection_detected: prompt injection in evidence text (treated as data): {hits[:3]}"))
