from __future__ import annotations

import csv
import gzip
import io
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable

from .identity import try_normalize_orgnr


def norm_key(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", s.lower())


def norm_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    value = str(value).strip()
    if not value or value.lower() in {"null", "none", "nan", "-", "ikke oppgitt"}:
        return None
    return value


def _truthy(value: Any) -> bool | None:
    if value is None or value == "": return None
    if isinstance(value, bool): return value
    v = str(value).strip().lower()
    if v in {"true", "ja", "yes", "1", "y"}: return True
    if v in {"false", "nei", "no", "0", "n"}: return False
    return None


def flatten_object(obj: Any, prefix: str = "", out: dict[str, Any] | None = None) -> dict[str, Any]:
    if out is None: out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict): flatten_object(v, key, out)
            elif isinstance(v, list):
                out[key] = v
                # simple dict values from addresses / codes also get dotted flattened paths
                for i, item in enumerate(v):
                    if isinstance(item, dict): flatten_object(item, f"{key}[{i}]", out)
            else: out[key] = v
    else:
        out[prefix or "value"] = obj
    return out


def _lookup(flat: dict[str, Any], *aliases: str) -> Any:
    indexed = {norm_key(k): v for k, v in flat.items()}
    for alias in aliases:
        key = norm_key(alias)
        if key in indexed and indexed[key] not in (None, "", [], {}):
            return indexed[key]
    # Some CSV exporters use nested flattened address field names.
    for alias in aliases:
        key = norm_key(alias)
        for k, v in indexed.items():
            if k.endswith(key) and v not in (None, "", [], {}):
                return v
    return None


def _address(flat: dict[str, Any], prefix: str) -> dict[str, Any] | None:
    fields = {
        "street": (f"{prefix}.adresse", f"{prefix}_adresse", f"{prefix}Adresse"),
        "postal_code": (f"{prefix}.postnummer", f"{prefix}_postnummer", f"{prefix}Postnummer"),
        "city": (f"{prefix}.poststed", f"{prefix}_poststed", f"{prefix}Poststed"),
        "municipality": (f"{prefix}.kommunenummer", f"{prefix}_kommunenummer", f"{prefix}Kommunenummer"),
        "country_code": (f"{prefix}.landkode", f"{prefix}_landkode", f"{prefix}Landkode"),
    }
    result = {k: norm_text(_lookup(flat, *aliases)) for k, aliases in fields.items()}
    return result if any(result.values()) else None


@dataclass
class RegistryRecord:
    organisation_number: str
    raw: dict[str, Any]
    values: dict[str, Any]



def normalize_registry_record(row: dict[str, Any], *, is_subentity: bool = False) -> RegistryRecord | None:
    flat = flatten_object(row)
    org = try_normalize_orgnr(_lookup(flat, "organisasjonsnummer", "orgnr", "organisation_number", "organization_number"))
    if not org: return None
    name = norm_text(_lookup(flat, "navn", "foretaksnavn", "name"))
    form = norm_text(_lookup(flat, "organisasjonsform.beskrivelse", "organisasjonsform", "organisasjonsform.kode", "org_form"))
    form_code = norm_text(_lookup(flat, "organisasjonsform.kode", "organisasjonsform_kode", "organisasjonskode"))
    industry = []
    for i in (1, 2, 3):
        code = norm_text(_lookup(flat, f"naeringskode{i}.kode", f"naeringskode{i}_kode", f"nace{i}", f"naeringskode{i}"))
        desc = norm_text(_lookup(flat, f"naeringskode{i}.beskrivelse", f"naeringskode{i}_beskrivelse", f"naering{i}_beskrivelse"))
        if code or desc:
            industry.append({"code": code, "description": desc, "rank": i})
    business_addr = _address(flat, "forretningsadresse") or _address(flat, "forretningsAdresse")
    postal_addr = _address(flat, "postadresse") or _address(flat, "postAdresse")
    location_addr = _address(flat, "beliggenhetsadresse") or _address(flat, "beliggenhetsAdresse")
    main_parent = norm_text(_lookup(flat, "overordnetEnhet", "overordnet_enhet", "overordnetEnhet.organisasjonsnummer")) if is_subentity else None
    values: dict[str, Any] = {
        "official_name": name,
        "organisation_number": org,
        "legal_form": {"code": form_code, "description": form} if form_code or form else None,
        "industry_classifications": industry or None,
        "business_address": business_addr,
        "postal_address": postal_addr,
        "location_address": location_addr,
        "official_website": norm_text(_lookup(flat, "hjemmeside", "website", "url")),
        "email": norm_text(_lookup(flat, "epostadresse", "epost", "email")),
        "phone": norm_text(_lookup(flat, "telefon", "telefonnummer", "phone")),
        "registration_date": norm_text(_lookup(flat, "registreringsdatoEnhetsregisteret", "registreringsdato_enhetsregisteret", "registreringsdato")),
        "foundation_date": norm_text(_lookup(flat, "stiftelsesdato", "founding_date")),
        "business_registered_date": norm_text(_lookup(flat, "registrertIForetaksregisteretDato", "registrertIForetaksregisteret", "foretaksregisteretDato")),
        "vat_registered": _truthy(_lookup(flat, "registrertIMvaregisteret", "registrert_i_mva_registeret", "mva")),
        "under_bankruptcy": _truthy(_lookup(flat, "konkurs", "underKonkursbehandling", "konkursbehandling")),
        "under_liquidation": _truthy(_lookup(flat, "underAvvikling", "underavvikling")),
        "under_compulsory_liquidation": _truthy(_lookup(flat, "underTvangsavviklingEllerTvangsopplosning", "underTvangsavvikling", "tvangsavvikling")),
        "deleted_date": norm_text(_lookup(flat, "slettedato", "avregistreringsdato")),
        "employees": norm_text(_lookup(flat, "antallAnsatte", "antall_ansatte", "employees")),
        "municipality": norm_text(_lookup(flat, "forretningsadresse.kommunenummer", "forretningsadresse_kommunenummer", "kommunenummer")),
        "parent_organisation_number": try_normalize_orgnr(main_parent) if main_parent else None,
        "group_flag": _truthy(_lookup(flat, "erIKonsern", "er_i_konsern")),
        "sector_code": norm_text(_lookup(flat, "institusjonellSektorkode.kode", "institusjonellSektorkode", "sektorkode")),
        "source_record_type": "sub_entity" if is_subentity else "entity",
    }
    status_parts = []
    if values["deleted_date"]: status_parts.append("deleted/removed from registry per registered date")
    if values["under_bankruptcy"]: status_parts.append("under bankruptcy proceedings")
    if values["under_liquidation"]: status_parts.append("under liquidation")
    if values["under_compulsory_liquidation"]: status_parts.append("under compulsory liquidation/dissolution")
    if status_parts: values["registration_status"] = "; ".join(status_parts)
    else:
        # Do not infer that the company is active unless the record explicitly provides status.
        registered = _truthy(_lookup(flat, "erSlettet", "slettet", "deleted"))
        values["registration_status"] = "deleted" if registered is True else ("registered; no adverse status flag in source" if registered is False else None)
    return RegistryRecord(org, row, values)


def _read_json_records(raw: bytes) -> list[dict[str, Any]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    # Handles JSON array, object wrappers, and JSON Lines.
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list): return [x for x in parsed if isinstance(x, dict)]
        if isinstance(parsed, dict):
            for key in ("_embedded", "_embedded.enheter", "enheter", "items", "data", "content", "_embedded.enhet"):  # tolerant of API variants
                value = parsed.get(key)
                if isinstance(value, list): return [x for x in value if isinstance(x, dict)]
                if isinstance(value, dict):
                    for inner in value.values():
                        if isinstance(inner, list): return [x for x in inner if isinstance(x, dict)]
            if any(k in parsed for k in ("organisasjonsnummer", "orgnr")): return [parsed]
    except json.JSONDecodeError:
        pass
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line: continue
        try:
            item = json.loads(line)
            if isinstance(item, dict): rows.append(item)
        except json.JSONDecodeError:
            continue
    return rows


def read_registry_blob(raw: bytes, wanted: set[str], *, is_subentity: bool = False) -> dict[str, RegistryRecord]:
    """Parse plain/gzip JSON or CSV and retain only requested organisation numbers."""
    if raw.startswith(b"\x1f\x8b"):
        try:
            raw_stream = gzip.GzipFile(fileobj=io.BytesIO(raw), mode="rb")
            # CSV endpoint is gzip-compressed. Peek without materialising the expanded dataset.
            prefix = raw_stream.peek(4096)[:4096]
            if prefix.lstrip().startswith((b"{", b"[")):
                raw = raw_stream.read()
            else:
                wrapper = io.TextIOWrapper(raw_stream, encoding="utf-8-sig", errors="replace", newline="")
                reader = csv.DictReader(wrapper)
                out: dict[str, RegistryRecord] = {}
                for row in reader:
                    rec = normalize_registry_record(row, is_subentity=is_subentity)
                    if rec and rec.organisation_number in wanted:
                        out[rec.organisation_number] = rec
                return out
        except (OSError, EOFError):
            pass
    text_prefix = raw[:4096].lstrip()
    if text_prefix.startswith((b"{", b"[")):
        records = _read_json_records(raw)
    else:
        try:
            textio = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8-sig", errors="replace", newline="")
            reader = csv.DictReader(textio)
            records = []
            out: dict[str, RegistryRecord] = {}
            for row in reader:
                rec = normalize_registry_record(row, is_subentity=is_subentity)
                if rec and rec.organisation_number in wanted: out[rec.organisation_number] = rec
            return out
        except csv.Error:
            return {}
    out = {}
    for row in records:
        rec = normalize_registry_record(row, is_subentity=is_subentity)
        if rec and rec.organisation_number in wanted: out[rec.organisation_number] = rec
    return out


def registry_api_payload_to_record(payload: dict[str, Any], orgnr: str) -> RegistryRecord | None:
    # The single-entity endpoint may return a HAL envelope around the entity fields.
    for key in ("_embedded", "enhet", "entity"):
        inner = payload.get(key)
        if isinstance(inner, dict):
            payload = inner
            break
    if "organisasjonsnummer" not in payload:
        payload = dict(payload)
        payload["organisasjonsnummer"] = orgnr
    record = normalize_registry_record(payload)
    # The request path is not sufficient proof: the returned entity must anchor to the exact ID.
    if record is None or record.organisation_number != orgnr:
        return None
    return record
