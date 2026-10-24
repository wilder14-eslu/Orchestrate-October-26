"""Content-addressed cache for text and structured data extracted from binary evidence.

Entries live in code/.cache/<namespace>/<sha256 of the file bytes>.json and are
committed with the code, so a machine without Tesseract (or without model API
access) reproduces exactly the same extraction. Namespaces: "text" (OCR) and
"vlm" (Phase 5 structured extraction).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional

DEFAULT_ROOT = Path(__file__).resolve().parent.parent / ".cache"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TextCache:
    def __init__(self, root: Path = DEFAULT_ROOT):
        self.root = root

    def _path(self, namespace: str, digest: str) -> Path:
        return self.root / namespace / f"{digest}.json"

    def get(self, namespace: str, digest: str) -> Optional[dict]:
        path = self._path(namespace, digest)
        if not path.exists():
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None  # a corrupt entry is treated as a miss and rebuilt
        return entry if entry.get("sha256") == digest else None

    def put(self, namespace: str, digest: str, entry: dict) -> None:
        path = self._path(namespace, digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"sha256": digest, **entry}
        path.write_text(json.dumps(payload, indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
