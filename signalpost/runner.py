from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import os
import re
import time
import urllib.parse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable

from .accounts import extract_financial_values, parse_years_payload, pdf_text, verify_account_identity
from .budget import BudgetExceeded, BudgetManager
from .config import ACCOUNTS_PDF, ACCOUNTS_YEARS, BRREG_BASE, LIMITS, REGISTRY_ENTITIES_CSV, REGISTRY_GROUP, REGISTRY_ROLES, REGISTRY_SUBENTITIES_CSV, REGISTRY_ENTITY, Limits
from .envelope import EnvelopeBuilder, now_iso
from .http_client import FetchResult, HTTPClient
from .identity import InvalidOrganisationNumber, normalize_orgnr, try_normalize_orgnr
from .registry import RegistryRecord, normalize_registry_record, read_registry_blob, registry_api_payload_to_record
from .store import StateStore, sha256_bytes, stable_json
from .summary import build_summary
from .website import RobotsPolicy, canonical_url, extract_page_facts, hostname, match_company_website, pick_followup_links


def utc_from_epoch(value: float) -> str:
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_organisation_input(path: str | Path) -> list[str]:
    """Read JSONL, JSON arrays/objects, CSV, or one organisation number per line."""
    text = Path(path).read_text(encoding="utf-8-sig")
    stripped = text.strip()
    if not stripped: return []
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, list): items = parsed
        elif isinstance(parsed, dict):
            items = None
            for key in ("organisations", "organizations", "companies", "organisation_numbers", "organization_numbers", "items", "data"):
                if isinstance(parsed.get(key), list): items = parsed[key]; break
            if items is None: items = [parsed]
        else: items = [parsed]
        vals = []
        for item in items:
            value = _item_orgnr(item)
            vals.append(value if value is not None else (stable_json(item) if isinstance(item, (dict, list)) else str(item or "")))
        return vals
    except json.JSONDecodeError:
        pass
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines: return []
    if "," in lines[0] and any(k in lines[0].casefold() for k in ("orgnr", "organisation", "organization")):
        import csv, io
        reader = csv.DictReader(io.StringIO(text))
        vals = []
        for row in reader:
            v = _item_orgnr(row)
            vals.append(v if v is not None else stable_json(row))
        return vals
    vals = []
    for line in lines:
        try:
            item = json.loads(line)
            value = _item_orgnr(item)
            if value is not None: vals.append(value); continue
        except json.JSONDecodeError:
            pass
        vals.append(line)
    return vals


def _item_orgnr(item: Any) -> str | None:
    if isinstance(item, (str, int)): return str(item).strip()
    if isinstance(item, dict):
        for key in ("organisation_number", "organization_number", "organisasjonsnummer", "orgnr", "company_number", "id"):
            if item.get(key) is not None: return str(item[key]).strip()
    return None


class ResearchRunner:
    def __init__(self, *, store: StateStore, budget: BudgetManager, limits: Limits = LIMITS,
                 run_id: str = "local-run", workers: int | None = None, skip_web: bool = False,
                 account_reports: bool = True):
        self.store = store
        self.budget = budget
        self.limits = limits
        self.run_id = run_id
        self.workers = max(1, min(workers or limits.workers, 24))
        self.skip_web = skip_web
        self.account_reports = account_reports
        self.http = HTTPClient(budget, timeout=limits.read_timeout_seconds, retries=limits.retries, max_redirects=limits.max_redirects)
        self.robots = RobotsPolicy(self.http, self.store)
        self.started_at = now_iso()
        self._bulk_evidence: dict[str, dict[str, Any]] = {}
        self._bulk_meta: dict[str, dict[str, Any]] = {}

    def _load_bulk(self, *, label: str, url: str, local_path: str | None, wanted: set[str]) -> dict[str, RegistryRecord]:
        cache_root = self.store.db_path.parent / "cache"
        cache_root.mkdir(parents=True, exist_ok=True)
        cache_path = cache_root / ("entities.csv.gz" if label == "entities" else "subentities.csv.gz")
        meta_path = cache_path.with_suffix(cache_path.suffix + ".json")
        provided = local_path is not None
        fetched_at = now_iso()
        if local_path:
            src = Path(local_path)
            if not src.exists():
                raise FileNotFoundError(f"provided {label} bulk file does not exist: {src}")
            body = src.read_bytes()
            source_url = url
            source_class = "official_registry_bulk_snapshot"
        else:
            body = b""
            source_url = url
            source_class = "official_registry_bulk"
            if cache_path.exists() and meta_path.exists():
                try:
                    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
                    age = time.time() - float(metadata.get("fetched_epoch", 0))
                    if age < 6 * 3600:
                        body = cache_path.read_bytes()
                        fetched_at = metadata.get("fetched_at", fetched_at)
                        self.budget.record_cache_hit()
                except (ValueError, OSError, json.JSONDecodeError):
                    body = b""
            if not body:
                response = self.http.fetch(url, max_bytes=self.limits.max_registry_bytes)
                if not response.ok:
                    self._bulk_meta[label] = {"url": url, "status": response.status, "error": response.error, "fetched_at": fetched_at}
                    return {}
                body = response.body
                fetched_at = now_iso()
                cache_path.write_bytes(body)
                meta_path.write_text(json.dumps({"fetched_at": fetched_at, "fetched_epoch": time.time(), "url": url, "sha256": sha256_bytes(body)}, indent=2), encoding="utf-8")
        digest = sha256_bytes(body)
        self._bulk_meta[label] = {"url": source_url, "status": 200, "error": None, "fetched_at": fetched_at, "content_sha256": digest, "source_class": source_class, "local_snapshot": provided}
        self._bulk_evidence[label] = {"body": body, "url": source_url, "source_class": source_class, "fetched_at": fetched_at, "content_sha256": digest, "status": 200, "content_type": "application/gzip; format=csv" if body.startswith(b"\x1f\x8b") else "text/csv"}
        return read_registry_blob(body, wanted, is_subentity=(label == "subentities"))

    def _fetch_cached(self, url: str, *, orgnr: str, ttl: int = 6 * 3600, max_bytes: int | None = None) -> FetchResult:
        cached = self.store.cache_get(url, ttl)
        if cached:
            self.budget.record_cache_hit(orgnr=orgnr)
            return FetchResult(url, url, cached["status"], cached["headers"], cached["body"], None, 0, 0, True, utc_from_epoch(float(cached["fetched_epoch"])))
        response = self.http.fetch(url, orgnr=orgnr, max_bytes=max_bytes or self.limits.max_html_bytes)
        if response.status is not None and response.error is None and response.status < 500:
            self.store.cache_put(url, response.status, response.headers, response.body, sha256_bytes(response.body))
        return response

    def _save_evidence(self, builder: EnvelopeBuilder, *, orgnr: str, source_url: str, source_class: str,
                       fetched_at: str, status: int | None, content_type: str | None, body: bytes,
                       excerpt: str, match: str = "verified", title: str | None = None,
                       published_at: str | None = None, confidence: float = 1.0,
                       validation_notes: str | None = None) -> tuple[str, dict[str, Any]]:
        snapshot = self.store.save_snapshot(organisation_number=orgnr, source_url=source_url, source_class=source_class,
                                            fetched_at=fetched_at, http_status=status, content_type=content_type,
                                            body=body, identity_match_status=match, excerpt=excerpt, run_id=self.run_id)
        builder.add_source_snapshot(snapshot)
        eid = builder.add_evidence(source_url=source_url, source_title=title or source_url, source_class=source_class,
                                   retrieved_at=fetched_at, content_sha256=snapshot["content_sha256"], claim_span=excerpt,
                                   identity_match_status=match, confidence=confidence, published_at=published_at,
                                   snapshot_id=snapshot["id"], http_status=status, content_type=content_type,
                                   validation_notes=validation_notes)
        return eid, snapshot

    def _add_registry_facts(self, builder: EnvelopeBuilder, record: RegistryRecord, source_label: str = "entities") -> dict[str, Any]:
        meta = self._bulk_evidence.get(source_label) or self._bulk_evidence.get("entities")
        identity = {"organisation_number": record.organisation_number, "official_name": record.values.get("official_name"),
                    "source_url": meta.get("url") if meta else f"{BRREG_BASE}/api/enheter/{record.organisation_number}",
                    "match_status": "verified", "confidence": 0.995, "evidence_ids": []}
        if meta:
            excerpt = stable_json({"organisation_number": record.organisation_number, "values": record.values})
            evidence_by_field: dict[str, list[str]] = {}
            # Each published fact receives an evidence item whose span contains the exact registry value.
            for field, value in record.values.items():
                if value is None or field == "source_record_type": continue
                eid, _ = self._save_evidence(builder, orgnr=record.organisation_number, source_url=meta["url"],
                    source_class=meta["source_class"], fetched_at=meta["fetched_at"], status=meta["status"],
                    content_type=meta.get("content_type"), body=meta["body"],
                    excerpt=f"Registry row for {record.organisation_number}; {field}={stable_json(value)}",
                    match="verified", title="Brønnøysund Register Centre open-data snapshot", confidence=0.995,
                    validation_notes="Value parsed from the row keyed by exact organisation number.")
                builder.add_claim(field, value, availability="available", confidence=0.995, evidence_ids=[eid],
                                  effective_date=record.values.get("deleted_date") if field == "registration_status" else None)
                evidence_by_field.setdefault(field, []).append(eid)
                identity["evidence_ids"].append(eid) if field in {"official_name", "organisation_number"} else None
            identity["evidence_ids"] = list(dict.fromkeys(identity["evidence_ids"]))
        return identity

    def _add_dynamic_registry_record(self, builder: EnvelopeBuilder, record: RegistryRecord, url: str, response: FetchResult) -> dict[str, Any]:
        fetched_at = now_iso()
        try: data = json.loads(response.body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError: data = {}
        eid, _ = self._save_evidence(builder, orgnr=record.organisation_number, source_url=url,
            source_class="official_registry_api", fetched_at=fetched_at, status=response.status,
            content_type=response.headers.get("content-type"), body=response.body,
            excerpt=f"Exact-organisation endpoint response for {record.organisation_number}: {stable_json(record.values)}",
            match="verified", title="Brønnøysund Register Centre entity endpoint", confidence=0.995)
        for field, value in record.values.items():
            if value is not None and field != "source_record_type":
                builder.add_claim(field, value, availability="available", confidence=0.995, evidence_ids=[eid])
        return {"organisation_number": record.organisation_number, "official_name": record.values.get("official_name"),
                "source_url": url, "match_status": "verified", "confidence": 0.995, "evidence_ids": [eid]}

    def _record_error(self, builder: EnvelopeBuilder, code: str, message: str, url: str | None = None, retryable: bool = False) -> None:
        builder.add_error(code=code, message=message, source_url=url, retryable=retryable)

    def _process_site(self, orgnr: str, record: RegistryRecord) -> dict[str, Any]:
        raw_url = record.values.get("official_website")
        site_url = canonical_url(str(raw_url)) if raw_url else None
        if not site_url:
            return {"orgnr": orgnr, "state": "not_available", "reason": "registry has no safely crawlable HTTPS website", "url": raw_url}
        allowed, robot_reason, robot_status = self.robots.may_fetch(site_url, orgnr=orgnr)
        if not allowed:
            return {"orgnr": orgnr, "state": "blocked", "reason": robot_reason, "url": site_url, "robots_status": robot_status}
        response = self._fetch_cached(site_url, orgnr=orgnr, ttl=6 * 3600)
        if not response.ok:
            if response.status in (401, 403, 407, 429) or response.error == "blocked": state = "blocked"
            elif response.status == 404 or response.error == "not_found": state = "not_available"
            else: state = "failed"
            return {"orgnr": orgnr, "state": state, "reason": response.error or f"HTTP {response.status}", "url": site_url, "response": response}
        facts = extract_page_facts(response.body, response.final_url)
        match_status, confidence, match_reason = match_company_website(record.values.get("official_name"), orgnr, response.body, facts["parser"])
        return {"orgnr": orgnr, "state": match_status, "reason": match_reason, "confidence": confidence,
                "url": site_url, "response": response, "facts": facts, "identity_match_status": match_status}

    def _apply_site_result(self, builder: EnvelopeBuilder, site: dict[str, Any], record: RegistryRecord,
                           followups: list[dict[str, Any]]) -> None:
        orgnr = site["orgnr"]
        url = site.get("url") or record.values.get("official_website") or ""
        if site["state"] == "not_available":
            builder.add_claim("website_content", None, availability="not_available", confidence=0.7, notes=site["reason"])
            return
        if site["state"] == "blocked":
            builder.add_claim("website_content", None, availability="blocked", confidence=0.9, notes=site["reason"])
            if site.get("response"):
                self._record_error(builder, "website_blocked", site["reason"], url)
            return
        if site["state"] == "failed":
            builder.add_claim("website_content", None, availability="failed", confidence=0.8, notes=site["reason"])
            self._record_error(builder, "website_fetch_failed", site["reason"], url, True)
            return
        response: FetchResult = site["response"]
        facts = site["facts"]
        verified = site["state"] == "verified"
        excerpt = f"Page title={facts.get('title')!r}; description={facts.get('description')!r}; match={site['reason']}"
        eid, snapshot = self._save_evidence(builder, orgnr=orgnr, source_url=response.final_url,
            source_class="company_owned_website", fetched_at=response.retrieved_at or now_iso(), status=response.status,
            content_type=response.headers.get("content-type"), body=response.body, excerpt=excerpt,
            match="verified" if verified else "ambiguous", title=facts.get("title") or response.final_url,
            confidence=site.get("confidence", 0.5), validation_notes=site["reason"])
        if not verified:
            builder.add_claim("website_identity_match", {"status": "ambiguous", "reason": site["reason"], "url": response.final_url},
                              availability="ambiguous", confidence=site.get("confidence", 0.35), evidence_ids=[eid])
            return
        builder.add_claim("website_content", {"url": response.final_url, "title": facts.get("title"), "description": facts.get("description")},
                          confidence=site.get("confidence", 0.9), evidence_ids=[eid])
        if facts.get("description"):
            builder.add_claim("website_description", facts["description"], confidence=site.get("confidence", 0.9), evidence_ids=[eid])
        if facts.get("title"):
            builder.add_claim("website_title", facts["title"], confidence=site.get("confidence", 0.9), evidence_ids=[eid])
        if facts.get("headings"):
            builder.add_claim("website_headings", facts["headings"][:8], confidence=0.88, evidence_ids=[eid],
                              notes="Heading text is reported as published; it is not interpreted as verified product performance.")
        # sameAs URLs are trusted only as outbound references linked from the verified official website.
        same_as = []
        for obj in facts.get("organization_jsonld", []):
            val = obj.get("sameAs")
            vals = val if isinstance(val, list) else [val]
            for s in vals:
                if isinstance(s, str) and s.startswith("https://"):
                    parsed = urllib.parse.urlsplit(s)
                    if parsed.hostname and not parsed.hostname.endswith((".invalid", ".example")):
                        same_as.append(s)
        if same_as:
            builder.add_claim("official_site_linked_profiles", sorted(set(same_as))[:20], confidence=0.9, evidence_ids=[eid],
                              notes="Links discovered on the verified company-controlled page; destination pages were not crawled.")
        if facts.get("social_links"):
            builder.add_claim("company_social_profiles", facts["social_links"][:15], confidence=0.95, evidence_ids=[eid],
                              notes="Verified company-owned social profiles discovered on the company-controlled website.")
            for soc in facts["social_links"][:10]:
                builder.add_claim(f"social_profile.{soc['platform']}", soc["url"], confidence=0.95, evidence_ids=[eid],
                                  notes=f"Company-owned {soc['platform']} profile linked from verified website.")
        if facts.get("emails"):
            builder.add_claim("website_contact_emails", facts["emails"][:5], confidence=0.92, evidence_ids=[eid],
                              notes="Direct contact email addresses published on verified website.")
        if facts.get("phones"):
            builder.add_claim("website_contact_phones", facts["phones"][:5], confidence=0.92, evidence_ids=[eid],
                              notes="Contact telephone numbers published on verified website.")
        jobs_published = 0
        name = record.values.get("official_name")
        for index, job in enumerate(facts.get("job_postings", [])[:20]):
            employer = job.get("hiringOrganization") or {}
            employer_name = employer.get("name") if isinstance(employer, dict) else employer
            if employer_name and not self._names_close(str(name or ""), str(employer_name)):
                continue
            title = job.get("title")
            if not title: continue
            value = {"title": str(title)[:300], "url": job.get("url") or response.final_url,
                     "date_posted": job.get("datePosted"), "valid_through": job.get("validThrough"),
                     "employment_type": job.get("employmentType"), "hiring_organisation": employer_name or name}
            job_eid, _ = self._save_evidence(builder, orgnr=orgnr, source_url=response.final_url,
                source_class="company_owned_website", fetched_at=response.retrieved_at or now_iso(), status=response.status,
                content_type=response.headers.get("content-type"), body=response.body,
                excerpt=f"Structured JobPosting JSON-LD: {stable_json(value)}", match="verified", title=str(title),
                confidence=0.93, published_at=str(value.get("date_posted")) if value.get("date_posted") else None,
                validation_notes="Employer name matched the registered company or is absent; page belongs to the registry-listed domain.")
            builder.add_claim(f"job_posting.{hashlib.sha256(stable_json(value).encode()).hexdigest()[:12]}", value,
                              confidence=0.93, evidence_ids=[job_eid], effective_date=value.get("date_posted"))
            jobs_published += 1
        if not jobs_published and any(x.get("kind") == "jobs" for x in pick_followup_links(facts.get("links", []), response.final_url)):
            builder.add_claim("job_postings_on_homepage", None, availability="not_available", confidence=0.7,
                              evidence_ids=[eid], notes="A careers/jobs link was found, but no JobPosting was embedded on the homepage; the job page may be checked separately.")
        for article in facts.get("articles", [])[:10]:
            title = article.get("headline") or article.get("name")
            date = article.get("datePublished") or article.get("dateCreated")
            link = article.get("url")
            if not (title and date): continue
            value = {"title": str(title)[:300], "date_published": str(date), "url": link or response.final_url}
            builder.add_claim(f"website_article.{hashlib.sha256(stable_json(value).encode()).hexdigest()[:12]}", value,
                              confidence=0.90, evidence_ids=[eid], effective_date=str(date),
                              notes="Structured article date as published by the official website; interpretation not inferred.")
        for link in pick_followup_links(facts.get("links", []), response.final_url, limit=12):
            link["orgnr"] = orgnr
            link["company_name"] = name
            link["home_evidence_id"] = eid
            link["root_url"] = response.final_url
            link["source_priority"] = int(link.get("priority", 0))
            followups.append(link)

    @staticmethod
    def _names_close(a: str, b: str) -> bool:
        def n(x: str) -> str:
            x = re.sub(r"\b(as|asa|ab|da|ltd|limited|inc|gmbh)\b", " ", x.lower())
            return re.sub(r"[^a-z0-9æøå]", "", x)
        na, nb = n(a), n(b)
        return bool(na and nb and (na == nb or na in nb or nb in na))

    def _process_followup(self, item: dict[str, Any]) -> dict[str, Any]:
        orgnr = item["orgnr"]
        allowed, reason, status = self.robots.may_fetch(item["url"], orgnr=orgnr)
        if not allowed: return {**item, "state": "blocked", "reason": reason, "robots_status": status}
        response = self._fetch_cached(item["url"], orgnr=orgnr, ttl=3 * 3600)
        if not response.ok:
            state = "blocked" if response.status in (401, 403, 407, 429) or response.error == "blocked" else ("not_available" if response.status == 404 else "failed")
            return {**item, "state": state, "reason": response.error or f"HTTP {response.status}", "response": response}
        facts = extract_page_facts(response.body, response.final_url)
        match_status, confidence, reason = match_company_website(item.get("company_name"), orgnr, response.body, facts["parser"])
        return {**item, "state": match_status, "reason": reason, "confidence": confidence, "response": response, "facts": facts}

    def _apply_followup(self, builder: EnvelopeBuilder, result: dict[str, Any]) -> None:
        if result.get("state") != "verified":
            if result.get("state") in {"blocked", "failed"}:
                self._record_error(builder, f"website_{result['state']}", result.get("reason", "follow-up not fetched"), result.get("url"), result.get("state") == "failed")
            return
        response: FetchResult = result["response"]
        facts = result["facts"]
        kind = result.get("kind", "page")
        excerpt = f"Page title={facts.get('title')!r}; description={facts.get('description')!r}; route category={kind}; identity={result.get('reason')}"
        eid, _ = self._save_evidence(builder, orgnr=result["orgnr"], source_url=response.final_url,
            source_class="company_owned_website", fetched_at=response.retrieved_at or now_iso(), status=response.status,
            content_type=response.headers.get("content-type"), body=response.body, excerpt=excerpt,
            match="verified", title=facts.get("title") or response.final_url,
            confidence=result.get("confidence", 0.9), validation_notes=result.get("reason"))
        if facts.get("title"):
            builder.add_claim(f"website_page.{kind}.{hashlib.sha256(response.final_url.encode()).hexdigest()[:10]}",
                              {"title": facts.get("title"), "url": response.final_url, "page_type": kind,
                               "description": facts.get("description")}, confidence=0.90, evidence_ids=[eid])
        for i, job in enumerate(facts.get("job_postings", [])[:25]):
            employer = job.get("hiringOrganization") or {}
            employer_name = employer.get("name") if isinstance(employer, dict) else employer
            if employer_name and not self._names_close(str(result.get("company_name") or ""), str(employer_name)):
                continue
            title = job.get("title")
            if not title: continue
            value = {"title": str(title)[:300], "url": job.get("url") or response.final_url,
                     "date_posted": job.get("datePosted"), "valid_through": job.get("validThrough"),
                     "employment_type": job.get("employmentType"), "hiring_organisation": employer_name or result.get("company_name")}
            job_eid, _ = self._save_evidence(builder, orgnr=result["orgnr"], source_url=response.final_url,
                source_class="company_owned_website", fetched_at=response.retrieved_at or now_iso(), status=response.status,
                content_type=response.headers.get("content-type"), body=response.body,
                excerpt=f"Structured JobPosting JSON-LD: {stable_json(value)}", match="verified", title=str(title),
                confidence=0.93, published_at=str(value.get("date_posted")) if value.get("date_posted") else None,
                validation_notes="Exact company match checked against the official company-name field.")
            builder.add_claim(f"job_posting.{hashlib.sha256(stable_json(value).encode()).hexdigest()[:12]}", value,
                              confidence=0.93, evidence_ids=[job_eid], effective_date=value.get("date_posted"))
        for article in facts.get("articles", [])[:10]:
            title = article.get("headline") or article.get("name")
            date = article.get("datePublished") or article.get("dateCreated")
            if not (title and date): continue
            val = {"title": str(title)[:300], "date_published": str(date), "url": article.get("url") or response.final_url}
            builder.add_claim(f"website_article.{hashlib.sha256(stable_json(val).encode()).hexdigest()[:12]}", val,
                              confidence=0.90, evidence_ids=[eid], effective_date=str(date))
        if facts.get("social_links"):
            for soc in facts["social_links"][:10]:
                builder.add_claim(f"social_profile.{soc['platform']}", soc["url"], confidence=0.94, evidence_ids=[eid],
                                  notes=f"Company-owned {soc['platform']} profile linked from verified {kind} page.")
        if facts.get("emails"):
            builder.add_claim("website_contact_emails", facts["emails"][:5], confidence=0.92, evidence_ids=[eid])
        if facts.get("phones"):
            builder.add_claim("website_contact_phones", facts["phones"][:5], confidence=0.92, evidence_ids=[eid])

    def _process_roles(self, orgnr: str) -> dict[str, Any]:
        url = REGISTRY_ROLES.format(orgnr=orgnr)
        response = self._fetch_cached(url, orgnr=orgnr, ttl=12 * 3600, max_bytes=2_000_000)
        if not response.ok:
            state = "blocked" if response.status in (401, 403, 407, 429) or response.error == "blocked" else ("not_available" if response.status == 404 else "failed")
            return {"orgnr": orgnr, "state": state, "url": url, "response": response, "roles": []}
        try: payload = json.loads(response.body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return {"orgnr": orgnr, "state": "failed", "url": url, "response": response, "roles": [], "reason": "invalid JSON response"}
        candidates: list[dict[str, Any]] = []
        def walk(obj: Any, inherited_role: str | None = None) -> None:
            if isinstance(obj, dict):
                t = obj.get("type")
                role_name = None
                if isinstance(t, dict): role_name = t.get("beskrivelse") or t.get("kode")
                elif isinstance(t, str): role_name = t
                for key in ("rolle", "rolleType", "rollebeskrivelse", "beskrivelse"):
                    if not role_name and isinstance(obj.get(key), str): role_name = obj[key]
                person = obj.get("person")
                company = obj.get("enhet") or obj.get("virksomhet")
                person_name = None
                if isinstance(person, dict):
                    person_name = person.get("navn")
                    if not person_name and isinstance(person.get("navn"), dict): person_name = person["navn"].get("sammensattnavn")
                    if not person_name:
                        parts = [person.get(k) for k in ("fornavn", "mellomnavn", "etternavn") if person.get(k)]
                        person_name = " ".join(str(x) for x in parts) if parts else None
                elif isinstance(person, str): person_name = person
                company_obj = None
                if isinstance(company, dict): company_obj = {"name": company.get("navn"), "organisation_number": company.get("organisasjonsnummer")}
                elif isinstance(company, str): company_obj = {"name": company}
                if role_name and (person_name or company_obj):
                    candidates.append({"role": str(role_name), "person_name": str(person_name) if person_name else None,
                                       "organisation": company_obj, "from_date": obj.get("fraDato") or obj.get("fradato"),
                                       "to_date": obj.get("tilDato") or obj.get("tildato"),
                                       "source_type": "person" if person_name else "organisation"})
                for k, v in obj.items():
                    if isinstance(v, (dict, list)): walk(v, role_name or inherited_role)
            elif isinstance(obj, list):
                for x in obj: walk(x, inherited_role)
        walk(payload)
        unique = []
        seen = set()
        for r in candidates:
            key = stable_json(r)
            if key not in seen: seen.add(key); unique.append(r)
        # Only keep person names attached to explicit role objects; sensitive personal IDs/DOBs are never copied.
        return {"orgnr": orgnr, "state": "available" if unique else "not_available", "url": url, "response": response, "roles": unique[:100]}

    def _apply_roles(self, builder: EnvelopeBuilder, result: dict[str, Any]) -> None:
        orgnr, state, url = result["orgnr"], result["state"], result["url"]
        response: FetchResult = result["response"]
        if state == "available":
            value = result["roles"]
            eid, _ = self._save_evidence(builder, orgnr=orgnr, source_url=response.final_url,
                source_class="official_registry_roles", fetched_at=response.retrieved_at or now_iso(), status=response.status,
                content_type=response.headers.get("content-type"), body=response.body,
                excerpt=f"Role records parsed for exact entity {orgnr}: {stable_json(value)[:1000]}", match="verified",
                title="Brønnøysund Register Centre roles endpoint", confidence=0.98,
                validation_notes="Endpoint path is scoped to the exact organisation number; personal identifiers are excluded.")
            builder.add_claim("registered_roles", value, confidence=0.98, evidence_ids=[eid])
            leadership_rx = re.compile(r"daglig leder|general manager|chief executive|ceo|styreleder|chair(?:man)? of the board|board member|styremedlem|leder", re.I)
            for role in value:
                if not leadership_rx.search(role.get("role", "")): continue
                builder.add_claim(f"leadership.{hashlib.sha256(stable_json(role).encode()).hexdigest()[:12]}", role,
                                  confidence=0.96, evidence_ids=[eid], effective_date=role.get("from_date"))
        elif state == "not_available":
            evidence_id, _ = self._save_evidence(builder, orgnr=orgnr, source_url=url, source_class="official_registry_roles",
                fetched_at=response.retrieved_at or now_iso(), status=response.status, content_type=response.headers.get("content-type"), body=response.body,
                excerpt="The organisation-scoped public roles endpoint returned no role collection (HTTP 200 empty or 404).",
                match="verified", title="Brønnøysund Register Centre roles endpoint", confidence=0.8)
            builder.add_claim("registered_roles", None, availability="not_available", confidence=0.8,
                              evidence_ids=[evidence_id], notes="No public role records were available from this endpoint at retrieval time.")
        else:
            builder.add_claim("registered_roles", None, availability=state, confidence=0.8, notes=result.get("reason") or response.error or f"HTTP {response.status}")
            self._record_error(builder, f"roles_{state}", result.get("reason") or response.error or f"HTTP {response.status}", url, state == "failed")

    def _process_group(self, orgnr: str) -> dict[str, Any]:
        url = REGISTRY_GROUP.format(orgnr=orgnr)
        response = self._fetch_cached(url, orgnr=orgnr, ttl=24 * 3600, max_bytes=4_000_000)
        if not response.ok:
            state = "blocked" if response.status in (401, 403, 407, 429) or response.error == "blocked" else ("not_available" if response.status == 404 else "failed")
            return {"orgnr": orgnr, "state": state, "url": url, "response": response, "relationships": []}
        try: payload = json.loads(response.body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError: return {"orgnr": orgnr, "state": "failed", "url": url, "response": response, "relationships": []}
        flat = []
        def walk(obj: Any, parent_name: str | None = None) -> None:
            if isinstance(obj, dict):
                n = obj.get("organisasjonsnummer") or obj.get("orgnr")
                if n:
                    valid = try_normalize_orgnr(n)
                    if valid:
                        flat.append({"organisation_number": valid, "name": obj.get("navn"), "relation_label": parent_name})
                label = obj.get("type") or obj.get("relasjon") or obj.get("rolle") or parent_name
                for k, v in obj.items():
                    if isinstance(v, (dict, list)): walk(v, str(label) if label else str(k))
            elif isinstance(obj, list):
                for item in obj: walk(item, parent_name)
        walk(payload)
        seen, rels = set(), []
        for item in flat:
            if item["organisation_number"] == orgnr: continue
            key = stable_json(item)
            if key not in seen: seen.add(key); rels.append(item)
        if orgnr not in {x.get("organisation_number") for x in flat}:
            # Query URL establishes scope, but a relationship graph that omits the subject is treated conservatively.
            return {"orgnr": orgnr, "state": "ambiguous", "url": url, "response": response, "relationships": rels, "reason": "response did not include queried organisation number"}
        return {"orgnr": orgnr, "state": "available" if rels else "not_available", "url": url, "response": response, "relationships": rels[:200]}

    def _apply_group(self, builder: EnvelopeBuilder, result: dict[str, Any]) -> None:
        response: FetchResult = result["response"]
        state = result["state"]
        if state == "available":
            value = result["relationships"]
            eid, _ = self._save_evidence(builder, orgnr=result["orgnr"], source_url=response.final_url,
                source_class="official_registry_group_structure", fetched_at=response.retrieved_at or now_iso(), status=response.status,
                content_type=response.headers.get("content-type"), body=response.body,
                excerpt=f"Corporate-group relationship records for {result['orgnr']}: {stable_json(value)[:2000]}",
                match="verified", title="Brønnøysund Register Centre corporate group structure", confidence=0.98)
            builder.add_claim("corporate_group_relationships", value, confidence=0.98, evidence_ids=[eid])
        elif state == "not_available":
            builder.add_claim("corporate_group_relationships", None, availability="not_available", confidence=0.8,
                              notes="No relationships were returned by the group endpoint for the queried entity.")
        else:
            builder.add_claim("corporate_group_relationships", result.get("relationships") or None,
                              availability=state, confidence=0.7, notes=result.get("reason") or response.error or f"HTTP {response.status}")
            self._record_error(builder, f"group_{state}", result.get("reason") or response.error or f"HTTP {response.status}", result["url"], state == "failed")

    def _process_accounts(self, orgnr: str, record: RegistryRecord) -> dict[str, Any]:
        years_url = ACCOUNTS_YEARS.format(orgnr=orgnr)
        year_response = self._fetch_cached(years_url, orgnr=orgnr, ttl=24 * 3600, max_bytes=2_000_000)
        if not year_response.ok:
            state = "blocked" if year_response.status in (401, 403, 407, 429) or year_response.error == "blocked" else ("not_available" if year_response.status == 404 else "failed")
            return {"orgnr": orgnr, "state": state, "url": years_url, "response": year_response, "years": []}
        years = parse_years_payload(year_response.body)
        if not years:
            return {"orgnr": orgnr, "state": "not_available", "url": years_url, "response": year_response, "years": []}
        chosen = years[0]
        pdf_url = ACCOUNTS_PDF.format(orgnr=orgnr, year=chosen)
        pdf_response = self._fetch_cached(pdf_url, orgnr=orgnr, ttl=7 * 24 * 3600, max_bytes=15_000_000)
        if not pdf_response.ok:
            state = "blocked" if pdf_response.status in (401, 403, 407, 429) or pdf_response.error == "blocked" else ("not_available" if pdf_response.status == 404 else "failed")
            return {"orgnr": orgnr, "state": state, "url": pdf_url, "response": pdf_response, "years": years, "year_response": year_response, "year": chosen}
        text = pdf_text(pdf_response.body)
        matched, reason = verify_account_identity(text, orgnr, record.values.get("official_name")) if text else (False, "PDF text extraction unavailable or document has no selectable text")
        vals = extract_financial_values(text) if matched else {}
        return {"orgnr": orgnr, "state": "available" if matched else "ambiguous", "url": pdf_url,
                "response": pdf_response, "years": years, "year_response": year_response, "year": chosen,
                "pdf_text": text, "matched": matched, "reason": reason, "financial_values": vals}

    def _apply_accounts(self, builder: EnvelopeBuilder, result: dict[str, Any]) -> None:
        orgnr, state = result["orgnr"], result["state"]
        if state not in {"available", "ambiguous"}:
            builder.add_claim("annual_accounts", None, availability=state, confidence=0.75,
                              notes="Annual-account years/PDF were checked but were not available for parsing.")
            self._record_error(builder, f"accounts_{state}", result.get("response").error or result.get("reason") or "Annual-account document unavailable", result.get("url"), state == "failed")
            return
        if "year_response" in result:
            year_response: FetchResult = result["year_response"]
            year_eid, _ = self._save_evidence(builder, orgnr=orgnr, source_url=ACCOUNTS_YEARS.format(orgnr=orgnr),
                source_class="official_annual_accounts_index", fetched_at=year_response.retrieved_at or now_iso(), status=year_response.status,
                content_type=year_response.headers.get("content-type"), body=year_response.body,
                excerpt=f"Official annual-account document years available: {result['years']}", match="verified",
                title="Brønnøysund Register Centre annual-account index", confidence=0.95)
            builder.add_claim("annual_account_years_available", result["years"], confidence=0.95, evidence_ids=[year_eid])
        response: FetchResult = result["response"]
        text = result.get("pdf_text", "")
        match = "verified" if state == "available" else "ambiguous"
        excerpt = f"PDF identity match: {result.get('reason')}; extracted text excerpt: {re.sub(r'\s+', ' ', text)[:1000]}"
        eid, _ = self._save_evidence(builder, orgnr=orgnr, source_url=response.final_url,
            source_class="official_annual_account_pdf", fetched_at=response.retrieved_at or now_iso(), status=response.status,
            content_type=response.headers.get("content-type") or "application/pdf", body=response.body,
            excerpt=excerpt, match=match, title=f"Official annual accounts {result.get('year')}", confidence=0.97 if state == "available" else 0.45,
            published_at=str(result.get("year")), validation_notes=result.get("reason"))
        builder.add_claim("annual_accounts", {"year": result.get("year"), "document_url": response.final_url,
                          "identity_match_status": match}, availability="available" if state == "available" else "ambiguous",
                          confidence=0.97 if state == "available" else 0.45, evidence_ids=[eid], reporting_period=str(result.get("year")),
                          notes=result.get("reason"))
        if state != "available": return
        for field, value in result.get("financial_values", {}).items():
            # The number is deliberately kept exactly as printed: scale/sign handling varies by report.
            builder.add_claim(f"financial.{field}", value, confidence=0.8, evidence_ids=[eid], reporting_period=str(result.get("year")),
                              notes="Raw amount as printed in the official PDF; not converted or normalized to whole NOK.")

    def _add_workplaces(self, builders: dict[str, EnvelopeBuilder], records: dict[str, RegistryRecord], subs: dict[str, RegistryRecord]) -> None:
        by_parent: dict[str, list[RegistryRecord]] = defaultdict(list)
        for sub in subs.values():
            parent = sub.values.get("parent_organisation_number")
            if parent in builders and sub.organisation_number not in records:
                by_parent[parent].append(sub)
        meta = self._bulk_evidence.get("subentities")
        if not meta: return
        for parent, children in by_parent.items():
            children.sort(key=lambda r: r.organisation_number)
            builder = builders[parent]
            workplace_values = []
            employee_sum = 0
            employee_known = False
            for child in children[:300]:
                v = child.values
                item = {"organisation_number": child.organisation_number, "name": v.get("official_name"),
                        "industry_classifications": v.get("industry_classifications"),
                        "business_address": v.get("business_address") or v.get("location_address"),
                        "employees": v.get("employees")}
                workplace_values.append(item)
                try:
                    count = int(str(v.get("employees")).replace(" ", ""))
                    if count >= 0:
                        employee_sum += count; employee_known = True
                except (TypeError, ValueError): pass
            eid, _ = self._save_evidence(builder, orgnr=parent, source_url=meta["url"],
                source_class=meta["source_class"], fetched_at=meta["fetched_at"], status=meta["status"],
                content_type=meta.get("content_type"), body=meta["body"],
                excerpt=f"Sub-entity rows linked to parent {parent}: {stable_json(workplace_values)[:2000]}",
                match="verified", title="Brønnøysund Register Centre sub-entity bulk snapshot", confidence=0.98,
                validation_notes="Rows are linked only by their registered overordnetEnhet organisation number.")
            builder.add_claim("registered_workplaces", workplace_values, confidence=0.98, evidence_ids=[eid],
                              notes="Only public sub-entities whose registered parent number exactly matches this organisation are included.")
            if employee_known:
                builder.add_claim("registered_workplace_employees_total", employee_sum, confidence=0.90, evidence_ids=[eid],
                                  notes="Sum of employee counts reported for listed sub-entities; this can differ from a consolidated employee number and excludes missing counts.")

    def _build_base(self, orgnr: str, started: str, record: RegistryRecord | None,
                    subrecord: RegistryRecord | None, bulk_ok: bool, error: str | None = None) -> tuple[EnvelopeBuilder, dict[str, Any] | None]:
        builder = EnvelopeBuilder(orgnr, self.run_id, started)
        identity = None
        if record:
            identity = self._add_registry_facts(builder, record)
            if record.values.get("source_record_type") == "sub_entity":
                builder.add_claim("entity_kind", "sub_entity", confidence=0.995, evidence_ids=identity.get("evidence_ids", []),
                                  notes="This organisation number is a registered sub-entity/workplace, not a top-level legal entity.")
            else:
                builder.add_claim("entity_kind", "legal_entity", confidence=0.995, evidence_ids=identity.get("evidence_ids", []))
        elif subrecord:
            identity = self._add_registry_facts(builder, subrecord, source_label="subentities")
            builder.add_claim("entity_kind", "sub_entity", confidence=0.995, evidence_ids=identity.get("evidence_ids", []),
                              notes="This organisation number identifies a registered sub-entity/workplace, not a top-level legal entity.")
            builder.add_claim("annual_accounts", None, availability="not_applicable", confidence=0.95,
                              notes="A registered sub-entity/workplace is not a top-level legal-entity account subject for this lookup.")
        elif error and error.startswith("identity_mismatch:"):
            builder.add_claim("official_registry_record", None, availability="ambiguous", confidence=0.0, notes=error)
            self._record_error(builder, "registry_identity_mismatch", error, REGISTRY_ENTITY.format(orgnr=orgnr), False)
        elif error:
            builder.add_claim("official_registry_record", None, availability="failed", confidence=0.0, notes=error)
            self._record_error(builder, "registry_lookup_failed", error, REGISTRY_ENTITY.format(orgnr=orgnr), True)
        elif bulk_ok:
            builder.add_claim("official_registry_record", None, availability="not_available", confidence=0.85,
                              notes="Neither the entity nor sub-entity bulk registry snapshot nor the exact-number lookup returned a record.")
            self._record_error(builder, "registry_record_not_found", "No public entity or sub-entity record was returned for this organisation number.", REGISTRY_ENTITY.format(orgnr=orgnr))
        return builder, identity

    def run_batch(self, input_values: list[str], *, bulk_path: str | None = None,
                  subentities_bulk_path: str | None = None, fixture_records: dict[str, dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        started_mono = time.monotonic()
        self.store.begin_run(self.run_id, self.started_at)
        input_values = list(input_values)
        valid_by_index: dict[int, str] = {}
        unique_valid: list[str] = []
        for i, value in enumerate(input_values):
            try:
                org = normalize_orgnr(value)
                valid_by_index[i] = org
                if org not in unique_valid: unique_valid.append(org)
            except InvalidOrganisationNumber:
                continue
        wanted = set(unique_valid)
        records: dict[str, RegistryRecord] = {}
        subs: dict[str, RegistryRecord] = {}
        bulk_ok = False
        load_errors: list[str] = []
        if fixture_records is not None:
            for orgnr in unique_valid:
                item = fixture_records.get(orgnr)
                if item:
                    v = dict(item)
                    v.setdefault("organisasjonsnummer", orgnr)
                    rec = normalize_registry_record(v)
                    if rec: records[orgnr] = rec
            # Fixture evidence is explicitly labelled non-production and must never masquerade as Brreg data.
            fixture_body = stable_json([{"organisation_number": k, "record": v.raw} for k, v in sorted(records.items())]).encode("utf-8")
            fixture_url = "https://example.invalid/signalpost-test-fixture/registry.json"
            self._bulk_evidence["entities"] = {"body": fixture_body, "url": fixture_url,
                "source_class": "local_test_fixture", "fetched_at": self.started_at,
                "content_sha256": sha256_bytes(fixture_body), "status": 200, "content_type": "application/json"}
            self._bulk_meta["entities"] = {"url": fixture_url, "status": 200, "error": None,
                "fetched_at": self.started_at, "content_sha256": sha256_bytes(fixture_body),
                "source_class": "local_test_fixture", "local_snapshot": True}
            bulk_ok = True
        else:
            try:
                records = self._load_bulk(label="entities", url=REGISTRY_ENTITIES_CSV, local_path=bulk_path, wanted=wanted)
                bulk_ok = bool(self._bulk_meta.get("entities", {}).get("status") == 200)
            except Exception as exc:
                load_errors.append(f"entity bulk: {type(exc).__name__}: {exc}")
            try:
                subs = self._load_bulk(label="subentities", url=REGISTRY_SUBENTITIES_CSV, local_path=subentities_bulk_path, wanted=wanted)
                bulk_ok = bulk_ok or bool(self._bulk_meta.get("subentities", {}).get("status") == 200)
            except Exception as exc:
                load_errors.append(f"sub-entity bulk: {type(exc).__name__}: {exc}")
        # Keep the authoritative bulk matches separate from exact API fallback matches so the evidence
        # for a dynamically recovered company always points to the exact-number response.
        bulk_records = dict(records)
        # An exact lookup is required for any valid identifier absent from a supposedly complete snapshot.
        missing = [org for org in unique_valid if org not in records and org not in subs]
        dynamic: dict[str, tuple[RegistryRecord | None, FetchResult | None, str | None]] = {}
        if fixture_records is None and missing and self.budget.requests < self.budget.max_requests:
            def direct_lookup(org: str):
                url = REGISTRY_ENTITY.format(orgnr=org)
                try:
                    response = self._fetch_cached(url, orgnr=org, ttl=4 * 3600, max_bytes=2_000_000)
                except BudgetExceeded as exc:
                    return org, None, None, str(exc)
                if response.ok:
                    try: payload = json.loads(response.body.decode("utf-8", errors="replace"))
                    except json.JSONDecodeError: return org, None, response, "registry endpoint returned invalid JSON"
                    rec = registry_api_payload_to_record(payload, org)
                    if rec is None:
                        observed = None
                        for key in ("organisasjonsnummer", "orgnr"):
                            if payload.get(key) is not None: observed = str(payload.get(key)); break
                        error_text = f"identity_mismatch: expected organisation number {org}, observed {observed or 'missing or malformed'}"
                        return org, None, response, error_text
                    return org, rec, response, None
                if response.status == 404:
                    return org, None, response, None
                return org, None, response, response.error or f"HTTP {response.status}"
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                futures = [pool.submit(direct_lookup, org) for org in missing]
                for future in as_completed(futures):
                    try:
                        org, record, response, error = future.result()
                        dynamic[org] = (record, response, error)
                        if record: records[org] = record
                    except Exception as exc:
                        # Per-company failure must not abort the remainder of the batch.
                        continue
        builders: dict[str, EnvelopeBuilder] = {}
        identities: dict[str, dict[str, Any] | None] = {}
        base_records: dict[str, RegistryRecord] = {}
        for org in unique_valid:
            bulk_rec = bulk_records.get(org)
            sub = None if bulk_rec else subs.get(org)
            dynamic_entry = dynamic.get(org)
            if dynamic_entry and dynamic_entry[0] is not None and bulk_rec is None and sub is None:
                # Build an empty base first: never attach bulk-dataset evidence to a row recovered from a different endpoint.
                builder, identity = self._build_base(org, self.started_at, None, None, False,
                    error=dynamic_entry[2])
                rec = dynamic_entry[0]
                response = dynamic_entry[1]
                if response is not None:
                    identity = self._add_dynamic_registry_record(builder, rec, REGISTRY_ENTITY.format(orgnr=org), response)
            else:
                rec = bulk_rec
                builder, identity = self._build_base(org, self.started_at, rec, sub,
                    bulk_ok=(bulk_ok and not (dynamic_entry and dynamic_entry[1] and dynamic_entry[1].status == 404)),
                    error=(dynamic_entry[2] if dynamic_entry and dynamic_entry[2] else ("; ".join(load_errors) if load_errors and not bulk_ok else None)))
            # A 200 response containing a different/missing organisation number is retained as ambiguous evidence,
            # but none of its business fields is allowed into the target profile.
            if dynamic_entry and dynamic_entry[2] and dynamic_entry[2].startswith("identity_mismatch:") and dynamic_entry[1]:
                response = dynamic_entry[1]
                eid, _ = self._save_evidence(builder, orgnr=org, source_url=response.final_url,
                    source_class="official_registry_api", fetched_at=response.retrieved_at or now_iso(),
                    status=response.status, content_type=response.headers.get("content-type"), body=response.body,
                    excerpt=dynamic_entry[2], match="ambiguous", title="Brønnøysund Register Centre exact entity lookup",
                    confidence=0.0, validation_notes="Response organisation number did not match the requested identifier; response facts were rejected.")
                builder.add_claim("registry_identity_validation", {"status": "ambiguous", "reason": dynamic_entry[2]},
                    availability="ambiguous", confidence=0.0, evidence_ids=[eid])
            # A direct 404 is an evidence-backed negative lookup; absence from a partial/failed crawl is not.
            if dynamic_entry and dynamic_entry[0] is None and dynamic_entry[1] and dynamic_entry[1].status == 404:
                response = dynamic_entry[1]
                eid, _ = self._save_evidence(builder, orgnr=org, source_url=response.final_url, source_class="official_registry_api",
                    fetched_at=response.retrieved_at or now_iso(), status=404, content_type=response.headers.get("content-type"), body=response.body,
                    excerpt=f"Exact-number lookup for {org} returned HTTP 404.", match="verified", title="Brønnøysund Register Centre exact entity lookup", confidence=0.9)
                builder.add_claim("official_registry_record", None, availability="not_available", confidence=0.9, evidence_ids=[eid])
            builders[org] = builder; identities[org] = identity
            if rec: base_records[org] = rec
        # Registered workplaces come from the official sub-entity bulk file; use exact parent-number matching.
        if fixture_records is None:
            self._add_workplaces(builders, base_records, subs)

        # Homepages are selected by unique registered domain, with stable priority across runs.
        followups: list[dict[str, Any]] = []
        site_results: list[dict[str, Any]] = []
        if not self.skip_web and fixture_records is None:
            site_candidates = []
            for org in unique_valid:
                record = base_records.get(org)
                if not record or not record.values.get("official_website"): continue
                url = canonical_url(str(record.values["official_website"]))
                if not url: continue
                # Avoid assigning one corporate domain's content to multiple legal entities in the same batch.
                site_candidates.append((org, record, url))
            unique_domains: dict[str, tuple[str, RegistryRecord, str]] = {}
            for org, record, url in sorted(site_candidates, key=lambda x: (x[0], x[2])):
                dom = hostname(url)
                if dom and dom not in unique_domains: unique_domains[dom] = (org, record, url)
            chosen_sites = list(unique_domains.values())[:self.limits.max_site_domains]
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                futures = [pool.submit(self._process_site, org, rec) for org, rec, _ in chosen_sites]
                for future in as_completed(futures):
                    try: site_results.append(future.result())
                    except BudgetExceeded as exc:
                        # budget exhaustion is represented by result failure, not a dropped organisation
                        continue
                    except Exception:
                        continue
            site_results.sort(key=lambda x: x["orgnr"])
            for site in site_results:
                builder = builders.get(site["orgnr"])
                record = base_records.get(site["orgnr"])
                if builder and record: self._apply_site_result(builder, site, record, followups)
            # Domains deliberately not visited are not marked as having no website information.

        # Roles are a high-value public registry signal. Use a hard stage cap, leaving room for page and accounts sources.
        roles_results: list[dict[str, Any]] = []
        if fixture_records is None and self.budget.requests < self.budget.max_requests:
            role_orgs = [org for org in unique_valid if org in base_records and base_records[org].values.get("source_record_type") != "sub_entity"]
            # A stable, round-robin sample preserves a spread across organisation numbers when the request budget is tight.
            role_orgs.sort()
            role_cap = min(500, len(role_orgs))
            role_orgs = role_orgs[:role_cap]
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                futures = [pool.submit(self._process_roles, org) for org in role_orgs]
                for future in as_completed(futures):
                    try: roles_results.append(future.result())
                    except BudgetExceeded: continue
                    except Exception: continue
            for result in sorted(roles_results, key=lambda x: x["orgnr"]):
                if result["orgnr"] in builders: self._apply_roles(builders[result["orgnr"]], result)

        # Use the official group endpoint only when the registry signals a group relationship.
        flagged = [org for org, rec in base_records.items() if rec.values.get("group_flag") is True]
        flagged.sort()
        group_results = []
        if fixture_records is None:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                futures = [pool.submit(self._process_group, org) for org in flagged[:self.limits.max_group_lookups]]
                for future in as_completed(futures):
                    try: group_results.append(future.result())
                    except (BudgetExceeded, Exception): continue
            for result in sorted(group_results, key=lambda x: x["orgnr"]):
                if result["orgnr"] in builders: self._apply_group(builders[result["orgnr"]], result)

        # High-value financial data is extracted only from the official account registry and only after PDF identity checks.
        account_results = []
        if self.account_reports and fixture_records is None and self.budget.requests < self.budget.max_requests:
            candidates = [org for org, rec in base_records.items()
                          if rec.values.get("source_record_type") != "sub_entity"
                          and (str((rec.values.get("legal_form") or {}).get("code") or "").upper() in {"AS", "ASA"}
                               or "aksjeselskap" in str((rec.values.get("legal_form") or {}).get("description") or "").lower())]
            candidates.sort()
            for org in candidates[:self.limits.max_account_reports]:
                if self.budget.requests + 2 > self.budget.max_requests: break
                try: account_results.append(self._process_accounts(org, base_records[org]))
                except BudgetExceeded: break
                except Exception as exc: account_results.append({"orgnr": org, "state": "failed", "url": ACCOUNTS_YEARS.format(orgnr=org), "response": FetchResult(ACCOUNTS_YEARS.format(orgnr=org), ACCOUNTS_YEARS.format(orgnr=org), None, error=str(exc)), "years": [], "reason": str(exc)})
            for result in account_results:
                if result["orgnr"] in builders: self._apply_accounts(builders[result["orgnr"]], result)

        # Fetch selected internal company pages for jobs, leadership, products and dated activity.
        if not self.skip_web and fixture_records is None and followups and self.budget.requests < self.budget.max_requests:
            followups.sort(key=lambda x: (-x.get("source_priority", 0), x.get("orgnr", ""), x["url"]))
            selected, seen_urls = [], set()
            for item in followups:
                key = (item["orgnr"], item["url"])
                if key in seen_urls: continue
                seen_urls.add(key); selected.append(item)
                if len(selected) >= self.limits.max_site_subpages: break
            followup_results = []
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                futures = [pool.submit(self._process_followup, item) for item in selected]
                for future in as_completed(futures):
                    try: followup_results.append(future.result())
                    except (BudgetExceeded, Exception): continue
            for result in sorted(followup_results, key=lambda x: (x["orgnr"], x["url"])):
                if result["orgnr"] in builders: self._apply_followup(builders[result["orgnr"]], result)

        # If earlier stages consumed fewer requests than planned, opportunistically expand role coverage.
        already_roles = {r["orgnr"] for r in roles_results}
        extra_orgs = [org for org in sorted(base_records) if org not in already_roles and base_records[org].values.get("source_record_type") != "sub_entity"]
        if fixture_records is None and self.budget.requests < self.budget.max_requests:
            for start in range(0, len(extra_orgs), self.workers):
                if self.budget.requests >= self.budget.max_requests - 1: break
                with ThreadPoolExecutor(max_workers=self.workers) as pool:
                    futures = [pool.submit(self._process_roles, org) for org in extra_orgs[start:start+self.workers]]
                    for future in as_completed(futures):
                        try:
                            result = future.result()
                            roles_results.append(result)
                            if result["orgnr"] in builders: self._apply_roles(builders[result["orgnr"]], result)
                        except (BudgetExceeded, Exception): continue

        # Finalize exactly one envelope per original input item, even if processing failed for that company.
        outputs = []
        now = now_iso()
        stats = self.budget.snapshot()
        for idx, original in enumerate(input_values):
            orgnr = valid_by_index.get(idx)
            if not orgnr:
                raw = str(original).strip()
                builder = EnvelopeBuilder(raw, self.run_id, self.started_at)
                builder.add_claim("identity_validation", None, availability="failed", confidence=0.0,
                                  notes="Input does not pass Norwegian organisation-number length and mod-11 checksum validation.")
                builder.add_error(code="invalid_organisation_number", message="Input was not a valid Norwegian organisation number.")
                invalid_summary = "Input organisation number could not be validated."
                builder.add_claim("summary_profile", {"text": invalid_summary}, availability="failed", confidence=0.0,
                                  notes="Input validation failed; no company facts were researched.")
                outputs.append(builder.finalize(terminal_status="completed", operations={"requests": 0, "runtime_ms": 0, "third_party_cost_usd": 0.0},
                                                completed_at=now, identity_anchor=None, summary=invalid_summary))
                continue
            builder = builders.get(orgnr)
            if builder is None:
                builder = EnvelopeBuilder(orgnr, self.run_id, self.started_at)
                builder.add_claim("research_result", None, availability="failed", confidence=0.0, notes="Internal batch runner failed to initialise this company's envelope.")
                builder.add_error(code="internal_company_failure", message="Company processing failed before final envelope construction.")
            identity = identities.get(orgnr)
            current = self.store.get_current_profile(orgnr)
            changes = self._diff(current, builder.claims)
            # Each factual summary sentence is composed from claims carrying evidence IDs.
            summary = build_summary(orgnr, builder.claims, identity, changes=changes)
            summary_evidence_ids = re.findall(r"ev-[A-Za-z0-9-]+", summary)
            summary_state = "available" if summary_evidence_ids else ("not_available" if any(c.get("availability") == "not_available" for c in builder.claims) else "failed")
            builder.add_claim("summary_profile", {"text": summary}, availability=summary_state,
                              confidence=0.9 if summary_evidence_ids else 0.6,
                              evidence_ids=list(dict.fromkeys(summary_evidence_ids)),
                              notes="Deterministic summary assembled from the claims in this envelope; references embedded in the text identify supporting evidence.")
            per_company_ops = {"requests": int(self.budget.per_org_requests.get(orgnr, 0)),
                               "runtime_ms": int(self.budget.per_org_latency_ms.get(orgnr, 0)),
                               "third_party_cost_usd": 0.0,
                               "cache_hits": int(self.budget.per_org_cache_hits.get(orgnr, 0)),
                               "retries": int(self.budget.per_org_retries.get(orgnr, 0))}
            envelope = builder.finalize(terminal_status=self._classify_terminal(builder), operations=per_company_ops,
                                         completed_at=now, identity_anchor=identity, summary=summary, changes=changes)
            self.store.save_profile(orgnr, self.run_id, envelope, now)
            outputs.append(envelope)
        report = {"run_id": self.run_id, "started_at": self.started_at, "completed_at": now,
                  "input_count": len(input_values), "unique_valid_organisation_numbers": len(unique_valid),
                  "invalid_input_count": len(input_values) - len(valid_by_index),
                  "output_count": len(outputs),
                  "unique_output_count": len({x.get("organisation_number") for x in outputs}),
                  "duplicate_input_count": len(outputs) - len({x.get("organisation_number") for x in outputs}),
                  "terminal_status_counts": self._terminal_counts(outputs), "operations": self.budget.snapshot(),
                  "limits": {"max_runtime_seconds": self.limits.max_runtime_seconds, "max_requests": self.limits.max_requests,
                             "max_external_cost_usd": self.limits.max_external_cost_usd},
                  "bulk_sources": self._bulk_meta, "source_counts": self._source_counts(outputs),
                  "live_registry_coverage_verified": fixture_records is None,
                  "qualification_passed": False,
                  "qualification_note": "A local structural smoke test is not an official Builderr evaluation; qualification is not claimed."}
        self.store.end_run(self.run_id, now, report)
        return outputs, report

    @staticmethod
    def _classify_terminal(builder: EnvelopeBuilder) -> str:
        """Run status follows the envelope contract; the six data states live on claims."""
        if any(error.get("code") == "internal_company_failure" for error in builder.errors):
            return "failed"
        return "completed"

    @staticmethod
    def _source_counts(outputs: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for item in outputs:
            for claim in item.get("claims", []):
                counts[claim.get("field", "unknown")] += 1
        return dict(counts)

    @staticmethod
    def _terminal_counts(outputs: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for o in outputs: counts[o.get("run", {}).get("terminal_status", "unknown")] += 1
        return dict(counts)

    @staticmethod
    def _diff(previous: dict[str, Any] | None, new_claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not previous: return [{"type": "added", "field": c.get("field"), "new_value": c.get("value"), "evidence_ids": c.get("evidence_ids", [])}
                                for c in new_claims if c.get("availability") == "available"]
        old_claims = previous.get("claims", [])
        old_by_field: dict[str, dict[str, Any]] = {}
        for c in old_claims:
            if c.get("availability") == "available": old_by_field.setdefault(c.get("field", ""), c)
        new_by_field: dict[str, dict[str, Any]] = {}
        for c in new_claims:
            if c.get("availability") == "available": new_by_field.setdefault(c.get("field", ""), c)
        changes = []
        for field, new in new_by_field.items():
            old = old_by_field.get(field)
            if old is None:
                changes.append({"type": "added", "field": field, "new_value": new.get("value"), "evidence_ids": new.get("evidence_ids", [])})
            elif stable_json(old.get("value")) != stable_json(new.get("value")):
                changes.append({"type": "changed", "field": field, "old_value": old.get("value"), "new_value": new.get("value"),
                                "old_evidence_ids": old.get("evidence_ids", []), "evidence_ids": new.get("evidence_ids", [])})
        # An absent field is not treated as removed: missing/blocked fetches do not prove deletion.
        # Derived summaries reflect underlying claims and should not become duplicate change events.
        return [item for item in changes if item.get("field") != "summary_profile"]
