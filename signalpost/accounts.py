from __future__ import annotations

import io
import re
from typing import Any

try:
    from pypdf import PdfReader
except Exception:  # PDF parsing remains optional when only the standard library is installed.
    PdfReader = None


def parse_years_payload(raw: bytes) -> list[int]:
    import json
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return sorted({int(x) for x in re.findall(rb"\b(20\d{2})\b", raw)}, reverse=True)
    found: set[int] = set()
    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if any(key in str(k).lower() for key in ("aar", "year", "regnskapsår", "regnskapsaar")):
                    if isinstance(v, (int, str)) and re.fullmatch(r"20\d{2}", str(v)):
                        found.add(int(v))
                    elif isinstance(v, list):
                        for x in v:
                            if isinstance(x, (int, str)) and re.fullmatch(r"20\d{2}", str(x)): found.add(int(x))
                            elif isinstance(x, dict): walk(x)
                if isinstance(v, (dict, list)): walk(v)
        elif isinstance(obj, list):
            for x in obj: walk(x)
        elif isinstance(obj, (int, str)) and re.fullmatch(r"20\d{2}", str(obj)):
            found.add(int(obj))
    walk(payload)
    return sorted((y for y in found if 2000 <= y <= 2100), reverse=True)


def pdf_text(raw: bytes, max_pages: int = 24) -> str:
    if PdfReader is None:
        return ""
    try:
        reader = PdfReader(io.BytesIO(raw), strict=False)
        pieces = []
        for page in reader.pages[:max_pages]:
            pieces.append(page.extract_text() or "")
        return "\n".join(pieces)[:500_000]
    except Exception:
        return ""


LABELS = {
    "revenue": (r"sum\s+driftsinntekter", r"driftsinntekter", r"salgsinntekter"),
    "operating_result": (r"driftsresultat",),
    "annual_result": (r"årsresultat", r"arsresultat", r"årets resultat", r"arets resultat"),
    "total_assets": (r"sum\s+eiendeler", r"sum eiendeler"),
    "equity": (r"sum\s+egenkapital", r"sum egenkapital"),
    "total_debt": (r"sum\s+gjeld",),
}


def extract_financial_values(text: str) -> dict[str, dict[str, str]]:
    """Conservative text parser: only accept labelled values visible in extracted PDF text."""
    out: dict[str, dict[str, str]] = {}
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    for field, patterns in LABELS.items():
        for i, line in enumerate(lines):
            low = line.casefold()
            if not any(re.search(p, low) for p in patterns): continue
            # Prefer amounts written on the same line; fallback to the next line only when it is numeric-like.
            candidates = [line]
            if i + 1 < len(lines): candidates.append(line + " " + lines[i + 1])
            got = None
            prior = None
            for cand in candidates:
                matches = re.findall(r"(?<![A-Za-z0-9])(?:NOK\s*)?([-−]?\d{1,3}(?:[ \u00a0]\d{3})*(?:[,\.]\d{1,2})?|[-−]?\d+)(?![A-Za-z0-9])", cand)
                # Don't take years or embedded label numbers as accounting values.
                clean = [m.strip() for m in matches if len(re.sub(r"\D", "", m)) >= 2 and not re.fullmatch(r"20\d{2}", re.sub(r"\D", "", m))]
                if clean:
                    # In annual-account statements with comparative columns, the newest filing period
                    # is listed in the first column, with the prior year following.
                    got = clean[0].replace("\u00a0", " ").strip()
                    if len(clean) >= 2:
                        prior = clean[1].replace("\u00a0", " ").strip()
                    break
            if got:
                res = {"amount_as_printed": got, "label_as_printed": line[:180]}
                if prior:
                    res["prior_year_amount"] = prior
                out[field] = res
                break
    return out


def compare_financial_periods(
    older_period: str,
    newer_period: str,
    older_values: dict[str, dict[str, str]],
    newer_values: dict[str, dict[str, str]],
    *,
    older_period_months: int = 12,
    newer_period_months: int = 12,
) -> dict[str, Any]:
    """Compare accounting figures between reporting periods.

    Strict rules:
    - Compare only periods of the same length (e.g. 12 months vs 12 months).
    - Sequence comparisons strictly older to newer.
    """
    if older_period_months != newer_period_months:
        return {
            "comparable": False,
            "reason": f"Reporting periods have different lengths ({older_period_months} months vs {newer_period_months} months); comparison invalid.",
            "older_period": str(older_period),
            "newer_period": str(newer_period),
            "order": "older_to_newer",
        }

    comparisons: dict[str, dict[str, str]] = {}
    for metric, n_data in newer_values.items():
        o_data = older_values.get(metric)
        if not o_data:
            continue
        o_val = o_data.get("amount_as_printed")
        n_val = n_data.get("amount_as_printed")
        if o_val and n_val:
            comparisons[metric] = {
                "older_period": str(older_period),
                "older_amount": o_val,
                "newer_period": str(newer_period),
                "newer_amount": n_val,
                "direction": "older_to_newer",
            }

    return {
        "comparable": True,
        "period_length_months": newer_period_months,
        "older_period": str(older_period),
        "newer_period": str(newer_period),
        "order": "older_to_newer",
        "metrics": comparisons,
    }


def verify_account_identity(text: str, orgnr: str, company_name: str | None) -> tuple[bool, str]:
    digits = re.sub(r"\D", "", text)
    if orgnr in digits:
        return True, "exact organisation number found in downloaded annual-account PDF"
    if company_name:
        norm = re.sub(r"[^a-z0-9æøå]", "", company_name.lower())
        body = re.sub(r"[^a-z0-9æøå]", "", text.lower())
        if norm and len(norm) >= 7 and norm in body:
            return True, "exact normalized registered company name found in PDF"
    return False, "PDF did not contain sufficient company identity evidence"
