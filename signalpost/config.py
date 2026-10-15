from __future__ import annotations

from dataclasses import dataclass

BRREG_BASE = "https://data.brreg.no/enhetsregisteret"
REGISTRY_ENTITIES_CSV = f"{BRREG_BASE}/api/enheter/lastned/csv"
REGISTRY_SUBENTITIES_CSV = f"{BRREG_BASE}/api/underenheter/lastned/csv"
REGISTRY_ENTITY = f"{BRREG_BASE}/api/enheter/{{orgnr}}"
REGISTRY_ROLES = f"{BRREG_BASE}/api/enheter/{{orgnr}}/roller"
REGISTRY_GROUP = f"{BRREG_BASE}/api/konsernstruktur/{{orgnr}}"
ACCOUNTS_YEARS = "https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/{orgnr}/aar"
ACCOUNTS_PDF = "https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/{orgnr}/{year}"
BRREG_LICENSE = "Norwegian Licence for Open Government Data (NLOD) 2.0"

@dataclass(frozen=True)
class Limits:
    max_runtime_seconds: int = 45 * 60
    max_requests: int = 2000
    max_external_cost_usd: float = 10.0
    connect_timeout_seconds: float = 8.0
    read_timeout_seconds: float = 12.0
    max_html_bytes: int = 2_000_000
    max_registry_bytes: int = 180_000_000
    max_redirects: int = 3
    retries: int = 2
    workers: int = 12
    max_site_domains: int = 450
    max_site_subpages: int = 250
    max_account_reports: int = 100
    max_group_lookups: int = 100

LIMITS = Limits()
