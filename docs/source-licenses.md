# Source, licence and permission notes

## Brønnøysund Register Centre (Brreg)

The implementation uses the public Enhetsregisteret API, entity/sub-entity CSV downloads, roles, group-structure records and public annual-account retrieval endpoints. The Brreg English API documentation lists the endpoints and states the Norwegian Licence for Open Government Data (NLOD) 2.0 for open data: <https://data.brreg.no/enhetsregisteret/api/dokumentasjon/en/index.html>.

## Company-owned websites and announcements

The website crawler follows the registry-listed official website, fetches over HTTPS, checks `robots.txt`, does not log in, does not bypass access controls or anti-bot systems, applies domain/request limits, and stores evidence excerpts rather than treating a full webpage as proof of every claim. The source site's terms, copyright, licence, access rules and any applicable rights continue to apply. Public accessibility alone is not a blanket redistribution licence. Do not publish the raw page snapshots outside the local audit workflow without reviewing those rights.

## Annual accounts

The Brreg Regnskapsregisteret public API exposes year-list and PDF-copy endpoints for annual accounts. This program extracts selected labelled figures from public PDFs but keeps the original printed amount and reporting year rather than guessing currency/scale. The source document and its terms govern the underlying document; users should not redistribute whole PDFs unless permitted.

## External models, APIs and dependencies

No LLM, paid search service or third-party company-data API is called. `pypdf==5.9.0` is the only runtime Python dependency; development-only schema tests use `jsonschema==4.26.0`. These packages are installed from their package indexes and are not vendored here. Review the upstream package licence notices as part of any redistributable container/build.

## Contact and responsibility

The repository does not invent a contact identity. Before official submission, the maintainer should include reachable contact details as requested by Builderr and monitor source complaints or corrections. Verify licence and access conditions again before adding connectors beyond the sources above.
