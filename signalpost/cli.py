from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from .budget import BudgetManager
from .config import LIMITS
from .fixtures import synthetic_registry_records
from .runner import ResearchRunner, read_organisation_input
from .store import StateStore, stable_json


def _json_load(path: str) -> Any:
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Evidence-backed Norwegian company research agent.")
    p.add_argument("--input", required=True, help="Input JSONL, JSON, CSV, or one organisation number per line")
    p.add_argument("--output", default="artifacts/results.jsonl", help="Output JSONL path (one envelope per input)")
    p.add_argument("--report", default="artifacts/run-report.json", help="Machine-readable run report path")
    p.add_argument("--state-dir", default="state", help="Persistent SQLite database and source snapshot directory")
    p.add_argument("--bulk", help="Optional local official enhetsregisteret CSV/GZIP snapshot")
    p.add_argument("--subentities-bulk", help="Optional local official underenheter CSV/GZIP snapshot")
    p.add_argument("--workers", type=int, default=LIMITS.workers)
    p.add_argument("--max-requests", type=int, default=LIMITS.max_requests)
    p.add_argument("--max-runtime-seconds", type=int, default=LIMITS.max_runtime_seconds)
    p.add_argument("--max-cost-usd", type=float, default=LIMITS.max_external_cost_usd)
    p.add_argument("--max-site-domains", type=int, default=LIMITS.max_site_domains)
    p.add_argument("--max-site-subpages", type=int, default=LIMITS.max_site_subpages)
    p.add_argument("--max-account-reports", type=int, default=LIMITS.max_account_reports)
    p.add_argument("--no-web", action="store_true", help="Skip company-owned website crawling")
    p.add_argument("--no-accounts", action="store_true", help="Skip annual-account PDF lookups")
    p.add_argument("--fixture-json", help="Use an offline registry fixture mapping; disables live research requests")
    p.add_argument("--synthetic-fixtures", type=int, metavar="N", help="Generate N synthetic records for smoke tests (test only)")
    p.add_argument("--run-id", help="Optional stable run identifier; defaults to a timestamp-based ID")
    p.add_argument("--html-report", help="Optional path to write a readable HTML company research report")
    p.add_argument("--markdown-report", help="Optional path to write a readable Markdown company research report")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    input_path = Path(args.input)
    try:
        inputs = read_organisation_input(input_path)
    except Exception as exc:
        print(f"failed to read input {input_path}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    fixture_records = None
    if args.synthetic_fixtures is not None:
        fixture_records = synthetic_registry_records(args.synthetic_fixtures)
        # The generated count should align with the input batch to avoid accidentally masking missing fixtures.
        missing = sorted({x for x in inputs if x not in fixture_records})
        if missing:
            print(f"warning: {len(missing)} input IDs do not exist in generated synthetic fixtures", file=sys.stderr)
    elif args.fixture_json:
        try:
            fixture_records = _json_load(args.fixture_json)
            if not isinstance(fixture_records, dict):
                raise ValueError("fixture JSON must be an object keyed by organisation number")
        except Exception as exc:
            print(f"failed to read fixture data: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2

    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    output_path = Path(args.output)
    report_path = Path(args.report)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    limits = replace(LIMITS,
        max_runtime_seconds=max(1, args.max_runtime_seconds),
        max_requests=max(0, args.max_requests),
        max_external_cost_usd=max(0.0, args.max_cost_usd),
        workers=max(1, min(24, args.workers)),
        max_site_domains=max(0, args.max_site_domains),
        max_site_subpages=max(0, args.max_site_subpages),
        max_account_reports=max(0, args.max_account_reports))
    budget = BudgetManager(max_requests=limits.max_requests,
        max_runtime_seconds=limits.max_runtime_seconds,
        max_cost_usd=limits.max_external_cost_usd)
    run_id = args.run_id or time.strftime("signalpost-%Y%m%dT%H%M%SZ", time.gmtime())
    store = StateStore(state_dir / "signalpost.sqlite3", state_dir / "snapshots")
    try:
        runner = ResearchRunner(store=store, budget=budget, limits=limits, run_id=run_id,
            workers=limits.workers, skip_web=args.no_web or fixture_records is not None,
            account_reports=not args.no_accounts)
        outputs, report = runner.run_batch(inputs, bulk_path=args.bulk,
            subentities_bulk_path=args.subentities_bulk, fixture_records=fixture_records)
        with output_path.open("w", encoding="utf-8", newline="\n") as out:
            for result in outputs:
                out.write(stable_json(result) + "\n")
        report["artifacts"] = {"input": str(input_path), "output_jsonl": str(output_path),
                               "run_report": str(report_path), "database": str(state_dir / "signalpost.sqlite3")}
        report["mode"] = "synthetic_fixture_test" if args.synthetic_fixtures is not None else ("offline_fixture_test" if fixture_records is not None else "live_research")
        report["test_only"] = fixture_records is not None
        report["input_sha256"] = __import__("hashlib").sha256(input_path.read_bytes()).hexdigest()
        if args.html_report:
            from .report import generate_html_report
            html_p = Path(args.html_report)
            html_p.parent.mkdir(parents=True, exist_ok=True)
            html_p.write_text(generate_html_report(outputs, report), encoding="utf-8")
            report["artifacts"]["html_report"] = str(html_p)
        if args.markdown_report:
            from .report import generate_markdown_report
            md_p = Path(args.markdown_report)
            md_p.parent.mkdir(parents=True, exist_ok=True)
            md_p.write_text(generate_markdown_report(outputs, report), encoding="utf-8")
            report["artifacts"]["markdown_report"] = str(md_p)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except Exception as exc:
        print(f"run failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
    print(json.dumps({"outputs": len(outputs), "terminal_status_counts": report.get("terminal_status_counts"),
                      "requests": report.get("operations", {}).get("requests"), "runtime_ms": report.get("operations", {}).get("runtime_ms"),
                      "output": str(output_path), "report": str(report_path),
                      "qualification_passed": report.get("qualification_passed", False)}, ensure_ascii=False))
    return 0 if len(outputs) == len(inputs) else 1
