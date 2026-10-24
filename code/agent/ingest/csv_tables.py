"""CSV tables of the dataset -> typed rows."""
from __future__ import annotations

import csv
from pathlib import Path
from typing import List, Type, TypeVar

from ..models import Activity, AuditChange, Identity, ManifestEntry, Request, Rule

T = TypeVar("T")


def read_rows(path: Path, cls: Type[T]) -> List[T]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        names = cls.__dataclass_fields__.keys()  # type: ignore[attr-defined]
        return [cls(**{k: (row.get(k) or "").strip() if k != "request_text" else (row.get(k) or "") for k in names})
                for row in reader]


def load_requests(path: Path) -> List[Request]:
    return read_rows(path, Request)


def load_tables(data_dir: Path):
    return (read_rows(data_dir / "user_identities.csv", Identity),
            read_rows(data_dir / "user_audit_log.csv", AuditChange),
            read_rows(data_dir / "system_activity.csv", Activity),
            read_rows(data_dir / "deletion_rules.csv", Rule),
            read_rows(data_dir / "evidence_files.csv", ManifestEntry))
