# Implementation plan

1. **Contract-first batch handling:** preserve every supplied input record and emit one terminal JSONL envelope per record; keep claim availability separate from run completion.
2. **Identity before enrichment:** validate Norwegian organisation numbers; prefer Brreg entity/sub-entity registry data; reject wrong-number responses and ambiguous company websites before publishing facts.
3. **Fact-level provenance:** bind claims to source-specific evidence containing source URL/title, retrieval and publication/effective dates where present, identity-check notes and SHA-256 content hashes.
4. **Bounded research:** reuse official bulk snapshots, use exact-number API fallback, crawl registry-listed official websites conservatively, and apply global/per-domain request, wall-clock and third-party cost budgets.
5. **Durable currentness:** store cached responses, content-addressed snapshots, current profiles and claim history in SQLite; detect supported material changes without treating failed/missing observations as deletions.
6. **Test and measure:** run offline unit/integration tests, JSON Schema checks, and 100/1,000-record synthetic throughput tests. Mark the result as unqualified until the actual Builderr starter and harness are available and pass.

The implementation followed this order. The remaining blocker is external contract reconciliation and a live real-company run, not a claim that official qualification has been achieved.
