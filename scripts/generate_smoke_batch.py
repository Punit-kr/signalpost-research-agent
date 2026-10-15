#!/usr/bin/env python3
"""Write deterministic synthetic input + fixture data for offline smoke testing only."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from signalpost.fixtures import synthetic_registry_records


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--count", type=int, default=100)
    p.add_argument("--out-dir", default="artifacts/smoke")
    args = p.parse_args()
    if args.count < 1:
        p.error("--count must be at least 1")
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    records = synthetic_registry_records(args.count)
    with (out / f"input-{args.count}.jsonl").open("w", encoding="utf-8") as f:
        for orgnr in records:
            f.write(json.dumps({"organisation_number": orgnr}, ensure_ascii=False) + "\n")
    (out / f"fixtures-{args.count}.json").write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"count": len(records), "input": str(out / f"input-{args.count}.jsonl"),
                      "fixtures": str(out / f"fixtures-{args.count}.json"), "synthetic": True}, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
