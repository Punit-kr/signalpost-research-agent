# Signalpost contract reconciliation status

## Confirmed from the public challenge page

- Builderr supplies the evaluation batch at run time; the agent must not substitute a self-selected company list.
- The result stream must contain exactly one terminal result per input company, even when information is unavailable or a source fails.
- Claim availability uses `available`, `not_available`, `blocked`, `not_applicable`, `ambiguous`, and `failed`.
- Facts should carry sources, retrieval dates and reporting periods where relevant; missing observations must not be converted to zero.
- Rerunning an unchanged source should be idempotent and should not create duplicate records or false change events.
- The public challenge asks submitters for a 100-company smoke result/run report, repository URL, exact commit hash, one command to run, service/model/licence disclosure, and expected cost.

Source: <https://builderr.ai/challenges/signalpost> (reviewed 15 October 2026).

## Confirmed from Brønnøysund's live documentation page

The current English API documentation is version 2.0.0 and lists direct entity lookup, roles, entity and sub-entity CSV downloads, group hierarchy, and update endpoints. The documentation states NLOD 2.0 for open data. The implementation uses the direct entity and roles endpoints plus entity/sub-entity downloads and group lookup.

Source: <https://data.brreg.no/enhetsregisteret/api/dokumentasjon/en/index.html> (reviewed 15 October 2026).

## Confirmed from the official evaluation contract and starter kit archive

- The official evaluation harness document (`https://builderr.ai/docs/signalpost-evaluation-harness.md`) was retrieved live and verified.
- The official starter kit archive (`https://builderr.ai/signalpost-starter-kit.tar.gz`) was downloaded and inspected.
- The official minimal output contract (`OUTPUT_CONTRACT.md`) requires:
  - Top-level keys: `organisation_number`, `run`, `claims`, `evidence`, `changes`, `errors`, `operations`.
  - Allowed claim availability states: `available`, `not_available`, `blocked`, `not_applicable`, `ambiguous`, `failed`.
  - Exact evidence object keys: `id`, `source_url`, `source_class`, `retrieved_at`, `content_sha256`, `claim_span`.
  - Operations object: `requests`, `runtime_ms`, `third_party_cost_usd`.
- Scoring breakdown confirmed: 50 Recall and coverage, 30 Precision and evidence, 12 Decision-useful synthesis, 8 UX and interaction (Qualification threshold: >= 65/100 on an official run).
- Fixed operational limits: 45 minutes runtime (2,700 seconds), maximum 2,000 outbound HTTP requests, maximum $10 declared external API spend.
- The envelope schema in `schema/output-envelope.schema.json` matches the official `OUTPUT_CONTRACT.md` format and passes validation.

