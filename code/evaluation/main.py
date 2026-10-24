"""Evaluation harness for the deletion-audit agent (stdlib only).

Usage (from the repository root):
    python code/evaluation/main.py                    # samples + custom cases + full-output invariants
    python code/evaluation/main.py --check-expected   # validate custom expected outputs only (no agent run)
    python code/evaluation/main.py --set-baseline     # store this run as the baseline for later deltas
    python code/evaluation/main.py --variant scored   # forwarded to the agent as --attribution scored
    python code/evaluation/main.py --determinism      # run the samples twice and compare bytes

The agent is called as a subprocess with this CLI contract:
    python code/main.py --data-dir <dir> --requests <csv> --out <csv> [--attribution strict|scored]

Gold answers (sample_output.csv) are read here only, never by the agent.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import custom_cases  # noqa: E402
import invariants  # noqa: E402
import metrics  # noqa: E402

REPO = HERE.parent.parent
DATASET = REPO / "dataset"
AGENT = REPO / "code" / "main.py"
RESULTS = HERE / "results"


# ---------------------------------------------------------------------------------- io helpers
def read_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def append_rows(path: Path, rows: list[dict]) -> None:
    """Rewrite a dataset CSV with extra rows, keeping its header order."""
    if not rows:
        return
    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        fields = list(reader.fieldnames or [])
        existing = list(reader)
    write_csv(path, fields, existing + rows)


def load_predictions(path: Path) -> dict[str, tuple[str, list | None, str]]:
    """case_id -> (status, steps or None if unparseable, raw)."""
    out = {}
    if not path.exists():
        return out
    for row in read_csv(path):
        try:
            steps = metrics.parse_steps(row.get("steps_json", ""))
        except (ValueError, json.JSONDecodeError):
            steps = None
        out[row.get("case_id", "")] = (row.get("deletion_status", ""), steps, row.get("steps_json", ""))
    return out


def evidence_times(data_dir: Path) -> dict[str, str]:
    times = {r["file_id"]: r["captured_at"] for r in read_csv(data_dir / "evidence_files.csv")}
    times.update({r["activity_id"]: r["event_time"] for r in read_csv(data_dir / "system_activity.csv")})
    times.update({r["audit_id"]: r["event_time"] for r in read_csv(data_dir / "user_audit_log.csv")})
    return times


# ---------------------------------------------------------------------------------- agent runner
def run_agent(data_dir: Path, requests: Path, out: Path, extra: list[str]) -> tuple[int, str]:
    if not AGENT.exists() or AGENT.stat().st_size == 0:
        return 127, "agent entry point code/main.py is missing or empty"
    cmd = [sys.executable, str(AGENT), "--data-dir", str(data_dir), "--requests", str(requests), "--out", str(out), *extra]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO)
    return proc.returncode, (proc.stderr or "")[-2000:]


# ---------------------------------------------------------------------------------- scoring
def score(gold: dict[str, tuple[str, list]], pred_path: Path) -> tuple[dict, dict]:
    pred = load_predictions(pred_path)
    per_case = {}
    for cid, (g_status, g_steps) in gold.items():
        p_status, p_steps, _ = pred.get(cid, ("MISSING", [], ""))
        per_case[cid] = metrics.case_metrics(g_status, g_steps, p_status, p_steps or [])
        per_case[cid]["pred_status"] = p_status
        per_case[cid]["gold_status"] = g_status
        per_case[cid]["pred_steps_raw"] = [metrics.step_key(s) for s in (p_steps or [])]
    return metrics.aggregate(per_case), per_case


def sample_gold() -> dict[str, tuple[str, list]]:
    return {r["case_id"]: (r["deletion_status"], metrics.parse_steps(r["steps_json"]))
            for r in read_csv(DATASET / "sample_output.csv")}


def build_custom_dataset(case: dict, root: Path) -> tuple[Path, Path]:
    """Copy dataset/ to root and overlay the case's fictional rows and files."""
    data_dir = root / "dataset"
    shutil.copytree(DATASET, data_dir, ignore=shutil.ignore_patterns("output.csv"))
    known = {r["email"].lower() for r in read_csv(data_dir / "user_identities.csv")}
    clash = [i["email"] for i in case.get("identities", []) if i["email"].lower() in known]
    if clash:
        raise ValueError(f"{case['id']}: custom identity collides with dataset: {clash}")
    append_rows(data_dir / "user_identities.csv", case.get("identities", []))
    append_rows(data_dir / "user_audit_log.csv", case.get("audit_log", []))
    append_rows(data_dir / "system_activity.csv", case.get("activity", []))
    manifest = []
    for f in case.get("files", []):
        target = data_dir / f["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f["content"], encoding="utf-8", newline="\n")
        manifest.append({"file_id": f["file_id"], "file_path": f["path"], "origin": f["origin"], "captured_at": f["captured_at"]})
    append_rows(data_dir / "evidence_files.csv", manifest)
    requests = root / "custom_requests.csv"
    write_csv(requests, list(case["requests"][0].keys()), case["requests"])
    return data_dir, requests


def check_expected() -> int:
    """Validate the hand-written expected outputs against the contract invariants."""
    bad = 0
    with tempfile.TemporaryDirectory() as tmp:
        for case in custom_cases.CASES:
            root = Path(tmp) / case["id"]
            data_dir, req_path = build_custom_dataset(case, root)
            exp = custom_cases.expected_rows(case)
            out = root / "expected.csv"
            rows = [{"case_id": cid, "deletion_status": st, "steps_json": json.dumps(steps, separators=(",", ":"))}
                    for cid, (st, steps) in sorted(exp.items(), key=lambda kv: invariants.natural_key(kv[0]))]
            write_csv(out, invariants.HEADER, rows)
            errs = invariants.check_output(str(out), case["requests"], evidence_times(data_dir))
            print(f"{case['id']:<34} {'OK' if not errs else 'INVALID'} {errs[:3] if errs else ''}")
            bad += bool(errs)
    gold_errs = invariants.check_output(str(DATASET / "sample_output.csv"), read_csv(DATASET / "sample_deletion_requests.csv"),
                                        evidence_times(DATASET))
    print(f"{'sample_output.csv (sanity)':<34} {'OK' if not gold_errs else gold_errs[:5]}")
    return 1 if bad or gold_errs else 0


def fmt(m: dict) -> str:
    if not m.get("cases"):
        return "no cases"
    return (f"cases={m['cases']} status={m['status_accuracy']:.3f} P={m['step_precision']:.3f} "
            f"R={m['step_recall']:.3f} F1={m['step_f1']:.3f} field={m['field_accuracy_mean']:.3f} seq={m['sequence_accuracy']:.3f}")


def delta(cur: dict, base: dict) -> str:
    keys = ["status_accuracy", "step_precision", "step_recall", "step_f1", "field_accuracy_mean", "sequence_accuracy"]
    return " ".join(f"{k}:{cur.get(k, 0) - base.get(k, 0):+.3f}" for k in keys if k in base)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check-expected", action="store_true")
    ap.add_argument("--set-baseline", action="store_true")
    ap.add_argument("--variant", choices=["strict", "scored"], default=None)
    ap.add_argument("--determinism", action="store_true")
    ap.add_argument("--full-output", default=str(REPO / "output.csv"), help="output.csv checked for invariants")
    ap.add_argument("--jobs", type=int, default=2, help="parallel custom-case runs")
    args = ap.parse_args()
    if args.check_expected:
        return check_expected()

    extra = ["--attribution", args.variant] if args.variant else []
    RESULTS.mkdir(exist_ok=True)
    report: dict = {"variant": args.variant or "default"}

    # 1) Public samples.
    sample_pred = RESULTS / "sample_predictions.csv"
    code, err = run_agent(DATASET, DATASET / "sample_deletion_requests.csv", sample_pred, extra)
    if code:
        print(f"[samples] agent failed ({code}): {err}")
    gold = sample_gold()
    report["samples"], sample_cases = score(gold, sample_pred)
    report["samples_invariants"] = invariants.check_output(str(sample_pred), read_csv(DATASET / "sample_deletion_requests.csv"),
                                                           evidence_times(DATASET))
    print("[samples]", fmt(report["samples"]))

    # 2) Custom cases, each on an isolated overlay copy of the dataset.
    custom_gold, custom_cases_scored = {}, {}
    with tempfile.TemporaryDirectory() as tmp:
        prepared = []
        for case in custom_cases.CASES:
            root = Path(tmp) / case["id"]
            data_dir, req_path = build_custom_dataset(case, root)
            prepared.append((case, data_dir, req_path, root / "pred.csv"))
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:  # each run is an isolated subprocess
            runs = list(pool.map(lambda p: run_agent(p[1], p[2], p[3], extra), prepared))
        for (case, _, _, out), (code, err) in zip(prepared, runs):
            exp = custom_cases.expected_rows(case)
            _, scored = score(exp, out)
            custom_gold.update(exp)
            custom_cases_scored.update(scored)
            ok = all(c["status_ok"] and c["sequence_ok"] for c in scored.values())
            print(f"[custom] {case['id']:<34} {'PASS' if ok else 'FAIL'}{'' if not code else f' (agent exit {code}: {err[-300:]})'}")
    report["custom"] = metrics.aggregate(custom_cases_scored)
    print("[custom] ", fmt(report["custom"]))

    # 3) Full-output invariants (no gold needed).
    full = Path(args.full_output)
    report["full_invariants"] = invariants.check_output(str(full), read_csv(DATASET / "deletion_requests.csv"), evidence_times(DATASET))
    print(f"[full]    {full.name}: {len(report['full_invariants'])} invariant violations", report["full_invariants"][:5])

    # 4) Determinism.
    if args.determinism:
        second = RESULTS / "sample_predictions_rerun.csv"
        run_agent(DATASET, DATASET / "sample_deletion_requests.csv", second, extra)
        same = sample_pred.exists() and second.exists() and \
            hashlib.sha256(sample_pred.read_bytes()).digest() == hashlib.sha256(second.read_bytes()).digest()
        report["deterministic"] = same
        print("[determinism]", "identical" if same else "DIFFERENT")

    # 5) Deltas and changed cases versus the stored baseline.
    per_case = {"samples": sample_cases, "custom": custom_cases_scored}
    base_path = RESULTS / "baseline.json"
    if base_path.exists() and not args.set_baseline:
        base = json.loads(base_path.read_text(encoding="utf-8"))
        print("[delta samples]", delta(report["samples"], base["report"]["samples"]))
        print("[delta custom] ", delta(report["custom"], base["report"]["custom"]))
        for group in ("samples", "custom"):
            for cid, c in per_case[group].items():
                b = base["per_case"][group].get(cid)
                if b and (b["pred_status"] != c["pred_status"] or b["pred_steps_raw"] != [list(x) for x in c["pred_steps_raw"]]):
                    print(f"[changed] {cid}: status {b['pred_status']} -> {c['pred_status']}, "
                          f"steps {len(b['pred_steps_raw'])} -> {len(c['pred_steps_raw'])}")
    payload = {"report": report, "per_case": per_case}
    (RESULTS / "latest.json").write_text(json.dumps(payload, indent=1, default=list), encoding="utf-8")
    if args.set_baseline:
        shutil.copy(RESULTS / "latest.json", base_path)
        print("[baseline] stored", base_path.relative_to(REPO))
    for cid, c in sorted({**sample_cases, **custom_cases_scored}.items(), key=lambda kv: invariants.natural_key(kv[0])):
        if not (c["status_ok"] and c["sequence_ok"]):
            print(f"  {cid}: {c['gold_status']} vs {c['pred_status']} missing={c['missing'][:3]} extra={c['extra'][:3]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
