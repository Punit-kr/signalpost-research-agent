# Local smoke-test results

**Important:** The inputs are synthetic checksum-valid test identifiers mapped to clearly labeled local fixtures. They are not real Norwegian companies, the run makes no external web requests, and the results do not count as company coverage for Signalpost. Both reports intentionally set `qualification_passed` to `false` and `live_research_completed` to `false`.

| Batch | Results | Unique IDs | Terminal statuses | HTTP requests | Third-party cost | Runtime | Report Artifacts |
|---:|---:|---:|---|---:|---:|---:|---|
| 100 | 100/100 | 100 | 100 completed | 0 | $0.00 | 330 ms | JSON, HTML (716 KB), MD (92 KB) |
| 1,000 | 1,000/1,000 | 1,000 | 1,000 completed | 0 | $0.00 | 20,890 ms | JSON, HTML (7.3 MB), MD (100 KB) |

### Structural verification on both output streams:
- 100% of envelopes validated against `schema/output-envelope.schema.json` without any schema errors.
- Every claim references valid existing evidence IDs (`evidence_ids` -> `evidence.id`).
- Every claim state is one of the six allowed values (`available`, `not_available`, `blocked`, `not_applicable`, `ambiguous`, `failed`).
- Evidence hashes are valid 64-character lowercase SHA-256 hex strings.
- Exactly one final result envelope per input organisation number (zero dropped, zero duplicated).
- Standalone HTML reports (`report-100.html`, `report-1000.html`) and Markdown reports (`report-100.md`, `report-1000.md`) are generated automatically for manual and automated UX inspection.

These synthetic batch tests exercise parsing, envelope cardinality, evidence references, snapshots, SQLite WAL persistence, thread safety, and scaling up to 1,000 profiles in ~20 seconds. They do not test live registry availability, real-company identity matching, actual company website coverage, or the official Builderr harness.
