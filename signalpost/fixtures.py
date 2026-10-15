"""Deterministic synthetic data for offline tests. Never use these IDs as real companies."""
from __future__ import annotations

from typing import Any

_WEIGHTS = (3, 2, 7, 6, 5, 4, 3, 2)


def _checksum(prefix: str) -> int | None:
    check = 11 - (sum(int(prefix[i]) * _WEIGHTS[i] for i in range(8)) % 11)
    if check == 11:
        check = 0
    return None if check == 10 else check


def synthetic_org_numbers(count: int, start: int = 80_000_000) -> list[str]:
    if count < 0:
        raise ValueError("count must be non-negative")
    out: list[str] = []
    candidate = start
    while len(out) < count:
        prefix = f"{candidate:08d}"
        candidate += 1
        if len(prefix) != 8:
            raise ValueError("synthetic organisation number prefix range exhausted")
        check = _checksum(prefix)
        if check is not None:
            out.append(prefix + str(check))
    return out


def synthetic_registry_records(count: int) -> dict[str, dict[str, Any]]:
    """Generate deliberately fake company records with checksum-valid synthetic IDs."""
    records: dict[str, dict[str, Any]] = {}
    for index, orgnr in enumerate(synthetic_org_numbers(count), start=1):
        records[orgnr] = {
            "organisasjonsnummer": orgnr,
            "navn": f"Synthetic Test Company {index:04d} AS",
            "organisasjonsform": {"kode": "AS", "beskrivelse": "Aksjeselskap (synthetic fixture)"},
            "naeringskode1": {"kode": "62.010", "beskrivelse": "Computer programming activities (synthetic fixture)"},
            "forretningsadresse": {
                "adresse": [f"Fixture Street {index}"],
                "postnummer": f"{1000 + index % 8999:04d}",
                "poststed": "TESTBY",
                "kommunenummer": "0301",
                "landkode": "NO",
            },
            "registreringsdatoEnhetsregisteret": "2020-01-02",
            "stiftelsesdato": "2019-12-15",
            "registrertIMvaregisteret": index % 2 == 0,
            "konkurs": False,
            "underAvvikling": False,
            "underTvangsavviklingEllerTvangsopplosning": False,
            "antallAnsatte": index % 80,
            "erSlettet": False,
            "hjemmeside": None,
        }
    return records
