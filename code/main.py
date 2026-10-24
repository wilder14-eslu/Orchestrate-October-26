"""Ghost Data Still Exists: deletion-audit agent (entry point).

Usage (from the repository root):
    python code/main.py                                   # audit dataset/deletion_requests.csv -> output.csv
    python code/main.py --requests dataset/sample_deletion_requests.csv --out sample_pred.csv
    python code/main.py --ingest-report                   # parse all evidence and print extraction statistics
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent.ingest import load_corpus  # noqa: E402


def ingest_report(data_dir: Path, ocr: bool) -> int:
    corpus = load_corpus(data_dir, ocr=ocr)
    print(f"evidence files in manifest: {len(corpus.manifest)}")
    print(f"{'format':<8}{'files':>7}{'observations':>14}{'failed':>8}")
    for suffix in sorted(set(corpus.stats["files"]) | set(corpus.stats["failed_files"])):
        print(f"{suffix:<8}{corpus.stats['files'].get(suffix, 0):>7}{corpus.stats['observations'].get(suffix, 0):>14}"
              f"{corpus.stats['failed_files'].get(suffix, 0):>8}")
    act = corpus.stats["activity_observations"]
    print(f"activity rows: {act['rows']} -> claim observations: {act['observations']}")
    print(f"capture scopes: {len(corpus.scopes)}  alias mentions: {len(corpus.alias_mentions)}")
    file_obs = [o for o in corpus.observations if o.source_kind != "ACTIVITY"]
    print("by presence/state:", dict(Counter((o.presence, o.observed_state) for o in file_obs).most_common()))
    print("by category:", dict(Counter(o.category for o in file_obs).most_common()))
    print(f"warnings: {len(corpus.warnings)}")
    for w in corpus.warnings:
        print(f"  [{w.evidence_id}] {w.path} @ {w.offset}: {w.message}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Deletion-audit agent")
    ap.add_argument("--data-dir", default=str(ROOT / "dataset"))
    ap.add_argument("--requests", default=None, help="requests CSV (default: <data-dir>/deletion_requests.csv)")
    ap.add_argument("--out", default=str(ROOT / "output.csv"))
    ap.add_argument("--attribution", choices=["strict", "scored"], default="scored")
    ap.add_argument("--no-ocr", action="store_true", help="do not read screenshots (default: cached OCR text, Tesseract on a miss)")
    ap.add_argument("--ingest-report", action="store_true")
    args = ap.parse_args()
    data_dir = Path(args.data_dir)
    if args.ingest_report:
        return ingest_report(data_dir, not args.no_ocr)
    print("decision pipeline not built yet (Phase 4 in progress); use --ingest-report", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
