from __future__ import annotations

import re
from typing import Any

WEIGHTS = (3, 2, 7, 6, 5, 4, 3, 2)

class InvalidOrganisationNumber(ValueError):
    pass


def normalize_orgnr(value: Any, *, verify_checksum: bool = True) -> str:
    """Normalize a Norwegian organisation number and verify its mod-11 digit."""
    if value is None:
        raise InvalidOrganisationNumber("organisation number is missing")
    raw = str(value).strip()
    digits = re.sub(r"[\s.-]", "", raw)
    if not re.fullmatch(r"\d{9}", digits):
        raise InvalidOrganisationNumber("organisation number must contain exactly 9 digits")
    if verify_checksum:
        weighted = sum(int(digits[i]) * WEIGHTS[i] for i in range(8))
        remainder = weighted % 11
        check = 11 - remainder
        if check == 11:
            check = 0
        if check == 10 or check != int(digits[8]):
            raise InvalidOrganisationNumber("organisation number has an invalid mod-11 check digit")
    return digits


def try_normalize_orgnr(value: Any) -> str | None:
    try:
        return normalize_orgnr(value)
    except InvalidOrganisationNumber:
        return None
