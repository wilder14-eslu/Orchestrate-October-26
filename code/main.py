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
from agent.pipeline import run  # noqa: E402


def ingest_report(data_dir: Path, ocr: bool) -> int:
    corpus = load_corpus(data_dir, ocr=ocr)
    print(f"evidence files in manifest: {len(corpus.manifest)}")
    print(f"{'format':<8}{'files':>7}{'observations':>14}{'crashes':>9}{'failed':>8}")
    all_suffixes = sorted(set(corpus.stats["files"]) | set(corpus.stats["failed_files"]) | set(corpus.stats.get("manifest_files", {})))
    for suffix in all_suffixes:
        print(f"{suffix:<8}{corpus.stats['files'].get(suffix, 0):>7}{corpus.stats['observations'].get(suffix, 0):>14}"
              f"{corpus.stats.get('parser_crashes', {}).get(suffix, 0):>9}"
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

    # Fail if 100% of files of any manifest file type crashed
    crashes = corpus.stats.get("parser_crashes", {})
    manifest_files = corpus.stats.get("manifest_files", {})
    for suffix, total in manifest_files.items():
        if total > 0 and crashes.get(suffix, 0) == total:
            print(f"[fatal] 100% of {suffix} files crashed during parsing ({total}/{total})", file=sys.stderr)
            return 2
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Deletion-audit agent")
    ap.add_argument("--data-dir", default=str(ROOT / "dataset"))
    ap.add_argument("--requests", default=None, help="requests CSV (default: <data-dir>/deletion_requests.csv)")
    ap.add_argument("--out", default=str(ROOT / "output.csv"))
    ap.add_argument("--attribution", choices=["strict", "scored"], default="scored")
    ap.add_argument("--no-ocr", action="store_true", help="do not read screenshots (default: cached OCR text, Tesseract on a miss)")
    ap.add_argument("--run-min-margin", type=float, default=2.0,
                    help="scored attribution of run events (not accounts): minimum best-minus-runner-up margin in log units")
    ap.add_argument("--vlm-refresh", action="store_true",
                    help="call Gemini (GEMINI_API_KEY, GEMINI_MODEL) for missing/stale screenshot and intent cache entries")
    ap.add_argument("--usage-out", default=None, help="write the measured model usage of this run to a JSON file")
    ap.add_argument("--usage-label", default="full_run", help="label stored in the usage file (e.g. cache_build, final_run)")
    ap.add_argument("--trace", default=None, help="write a per-case JSON trace (links, states, notes) to this path")
    ap.add_argument("--strict", action="store_true",
                    help="promote semantic warnings (such as COMPLETE with zero steps) to fatal errors (exit code 3)")
    ap.add_argument("--ingest-report", action="store_true")
    args = ap.parse_args()
    data_dir = Path(args.data_dir)
    if args.ingest_report:
        return ingest_report(data_dir, not args.no_ocr)
    requests = Path(args.requests) if args.requests else data_dir / "deletion_requests.csv"
    return run(data_dir, requests, Path(args.out), args.attribution, not args.no_ocr, Path(args.trace) if args.trace else None, args.run_min_margin,
               args.vlm_refresh, Path(args.usage_out) if args.usage_out else None, args.usage_label, args.strict)


if __name__ == "__main__":
    sys.exit(main())
