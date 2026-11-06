from __future__ import annotations

import gzip
import io
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from signalpost.accounts import extract_financial_values, verify_account_identity
from signalpost.budget import BudgetExceeded, BudgetManager
from signalpost.envelope import EnvelopeBuilder
from signalpost.fixtures import synthetic_org_numbers, synthetic_registry_records
from signalpost.http_client import HTTPClient, FetchResult, safe_public_url
from signalpost.identity import InvalidOrganisationNumber, normalize_orgnr
from signalpost.registry import normalize_registry_record, read_registry_blob, registry_api_payload_to_record
from signalpost.runner import ResearchRunner, read_organisation_input
from signalpost.store import StateStore, stable_json
from signalpost.website import hostname, match_company_website, PageParser


class IdentityTests(unittest.TestCase):
    def test_valid_synthetic_ids_and_separator_normalization(self):
        for number in synthetic_org_numbers(30):
            self.assertEqual(normalize_orgnr(number), number)
            self.assertEqual(normalize_orgnr(number[:3] + " " + number[3:]), number)

    def test_invalid_length_and_checksum_rejected(self):
        with self.assertRaises(InvalidOrganisationNumber):
            normalize_orgnr("123")
        valid = synthetic_org_numbers(1)[0]
        bad = valid[:-1] + str((int(valid[-1]) + 1) % 10)
        with self.assertRaises(InvalidOrganisationNumber):
            normalize_orgnr(bad)

    def test_api_mismatched_record_rejected(self):
        a, b = synthetic_org_numbers(2)
        self.assertIsNone(registry_api_payload_to_record({"organisasjonsnummer": b, "navn": "Wrong Company"}, a))
        self.assertIsNotNone(registry_api_payload_to_record({"organisasjonsnummer": a, "navn": "Right Company"}, a))


class RegistryParsingTests(unittest.TestCase):
    def test_gzip_csv_lookup_and_fields(self):
        org = synthetic_org_numbers(1)[0]
        data = ("organisasjonsnummer,navn,organisasjonsform.kode,organisasjonsform.beskrivelse,naeringskode1.kode,naeringskode1.beskrivelse,registreringsdatoEnhetsregisteret\n"
                f"{org},Example AS,AS,Aksjeselskap,62.010,Computer programming,2020-01-02\n")
        raw = gzip.compress(data.encode("utf-8"))
        parsed = read_registry_blob(raw, {org})
        self.assertIn(org, parsed)
        self.assertEqual(parsed[org].values["official_name"], "Example AS")
        self.assertEqual(parsed[org].values["legal_form"]["code"], "AS")
        self.assertEqual(parsed[org].values["industry_classifications"][0]["code"], "62.010")

    def test_subentity_parent_exactly_parsed(self):
        org, parent = synthetic_org_numbers(2)
        rec = normalize_registry_record({"organisasjonsnummer": org, "navn": "Workplace", "overordnetEnhet": parent}, is_subentity=True)
        self.assertEqual(rec.values["parent_organisation_number"], parent)


class SafetyAndBudgetTests(unittest.TestCase):
    def test_https_and_reserved_urls(self):
        self.assertTrue(safe_public_url("https://www.example.org/path")[0])
        self.assertFalse(safe_public_url("http://example.org")[0])
        self.assertFalse(safe_public_url("https://127.0.0.1")[0])
        self.assertFalse(safe_public_url("https://host.invalid")[0])
        self.assertFalse(safe_public_url("https://user:pass@example.org")[0])

    def test_request_and_domain_caps_are_hard(self):
        b = BudgetManager(max_requests=2, max_runtime_seconds=30, max_cost_usd=1.0, max_requests_per_domain=1)
        b.consume_request(host="one.example.org", orgnr="x")
        with self.assertRaises(BudgetExceeded):
            b.consume_request(host="one.example.org", orgnr="y")
        b.consume_request(host="two.example.org", orgnr="z")
        with self.assertRaises(BudgetExceeded):
            b.consume_request(host="three.example.org", orgnr="z")

    def test_third_party_cost_guard(self):
        b = BudgetManager(max_requests=2, max_runtime_seconds=30, max_cost_usd=1.0)
        b.record_cost(0.5)
        with self.assertRaises(BudgetExceeded):
            b.record_cost(0.6)

    def test_hostname_normalization_does_not_strip_arbitrary_characters(self):
        self.assertEqual(hostname("https://www.example.org"), "example.org")
        self.assertEqual(hostname("https://wwwindow.org"), "wwwindow.org")

    def test_similar_but_not_exact_company_name_is_ambiguous(self):
        p = PageParser("https://example.org/")
        p.title = "Northwind Holdings Group"
        state, confidence, _ = match_company_website("Northwind Holdings AS", "123456789", b"<html></html>", p)
        self.assertEqual(state, "ambiguous")
        self.assertLess(confidence, 0.5)


class _FakeResponse:
    def __init__(self, status=200, headers=None, body=b"ok"):
        self.status = status
        self.headers = headers or {"Content-Type": "text/plain"}
        self._body = body
    def read(self, limit=-1):
        return self._body[:limit] if limit >= 0 else self._body
    def close(self):
        return None


class HTTPClientTests(unittest.TestCase):
    def test_redirects_are_revalidated_and_counted(self):
        budget = BudgetManager(max_requests=5, max_runtime_seconds=30, max_cost_usd=1)
        client = HTTPClient(budget, retries=0, max_redirects=2)
        results = [_FakeResponse(302, {"Location": "https://example.org/final"}, b""), _FakeResponse(200, {"Content-Type": "text/plain"}, b"ready")]
        with patch.object(client._opener, "open", side_effect=results) as open_mock:
            result = client.fetch("https://example.org/start", orgnr="800000009")
        self.assertTrue(result.ok)
        self.assertEqual(result.final_url, "https://example.org/final")
        self.assertEqual(budget.requests, 2)
        self.assertEqual(open_mock.call_count, 2)

    def test_blocked_response_is_not_misreported_as_available(self):
        budget = BudgetManager(max_requests=5, max_runtime_seconds=30, max_cost_usd=1)
        client = HTTPClient(budget, retries=0)
        with patch.object(client._opener, "open", return_value=_FakeResponse(403, {}, b"blocked")):
            result = client.fetch("https://example.org/secret", orgnr="800000009")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "blocked")
        self.assertEqual(budget.blocked, 1)

    def test_server_error_retries_but_stays_within_request_budget(self):
        budget = BudgetManager(max_requests=2, max_runtime_seconds=30, max_cost_usd=1)
        client = HTTPClient(budget, retries=1)
        responses = [_FakeResponse(503, {}, b"retry"), _FakeResponse(200, {}, b"ok")]
        with patch.object(client._opener, "open", side_effect=responses), patch("signalpost.http_client.time.sleep"):
            result = client.fetch("https://example.org/data", orgnr="800000009")
        self.assertTrue(result.ok)
        self.assertEqual(budget.requests, 2)
        self.assertEqual(budget.retries, 1)
        self.assertEqual(budget.per_org_retries["800000009"], 1)

    def test_response_size_cap(self):
        budget = BudgetManager(max_requests=2, max_runtime_seconds=30, max_cost_usd=1)
        client = HTTPClient(budget, retries=0)
        with patch.object(client._opener, "open", return_value=_FakeResponse(200, {}, b"0123456789")):
            result = client.fetch("https://example.org/large", max_bytes=4)
        self.assertEqual(result.error, "response_too_large")
        self.assertLessEqual(len(result.body), 4)

    def test_timeout_is_retried_within_request_budget(self):
        budget = BudgetManager(max_requests=2, max_runtime_seconds=30, max_cost_usd=1)
        client = HTTPClient(budget, retries=1)
        with patch.object(client._opener, "open", side_effect=[socket.timeout(), _FakeResponse(200, {}, b"recovered")]), patch("signalpost.http_client.time.sleep"):
            result = client.fetch("https://example.org/timeout", orgnr="800000009")
        self.assertTrue(result.ok)
        self.assertEqual(budget.requests, 2)
        self.assertEqual(budget.retries, 1)


class AccountParserTests(unittest.TestCase):
    def test_labeled_financial_values_and_identity_are_evidence_bound(self):
        orgnr = synthetic_org_numbers(1)[0]
        name = "Example Nordic AS"
        text = (f"Annual account for {name}\nOrganisation number {orgnr}\n"
                "Sum driftsinntekter 1 234\nDriftsresultat -234\nÅrsresultat 123\nSum eiendeler 3 456")
        values = extract_financial_values(text)
        self.assertEqual(values["revenue"]["amount_as_printed"], "1 234")
        self.assertEqual(values["operating_result"]["amount_as_printed"], "-234")
        self.assertTrue(verify_account_identity(text, orgnr, name)[0])
        self.assertFalse(verify_account_identity("A different company and 1 234 NOK", orgnr, name)[0])


class StoreTests(unittest.TestCase):
    def test_snapshot_ids_do_not_collide_between_companies_or_urls(self):
        with tempfile.TemporaryDirectory() as d:
            store = StateStore(Path(d) / "db.sqlite3", Path(d) / "snaps")
            try:
                a, b = synthetic_org_numbers(2)
                kwargs = dict(source_class="test", fetched_at="2026-01-01T00:00:00Z", http_status=200,
                              content_type="application/json", body=b'{"same":true}', identity_match_status="verified",
                              excerpt="same test payload", run_id="t")
                one = store.save_snapshot(organisation_number=a, source_url="https://example.org/a", **kwargs)
                two = store.save_snapshot(organisation_number=b, source_url="https://example.org/a", **kwargs)
                three = store.save_snapshot(organisation_number=a, source_url="https://example.org/b", **kwargs)
                self.assertEqual(len({one["id"], two["id"], three["id"]}), 3)
                self.assertEqual(store.count_history(), 0)
            finally:
                store.close()

    def test_profile_hash_ignores_retrieval_timestamps(self):
        with tempfile.TemporaryDirectory() as d:
            store = StateStore(Path(d) / "db.sqlite3", Path(d) / "snaps")
            try:
                org = synthetic_org_numbers(1)[0]
                b1 = EnvelopeBuilder(org, "run-1", "2026-01-01T00:00:00Z")
                b1.add_claim("official_name", "Example AS", evidence_ids=["ev-1"])
                e1 = b1.finalize(terminal_status="available", operations={"requests": 1, "runtime_ms": 1, "third_party_cost_usd": 0},
                                completed_at="2026-01-01T00:00:01Z", identity_anchor={"organisation_number": org, "official_name": "Example AS", "match_status": "verified"})
                one = store.save_profile(org, "run-1", e1, "2026-01-01T00:00:01Z")
                e2 = dict(e1); e2["run"] = {**e1["run"], "run_id": "run-2", "started_at": "2026-01-02T00:00:00Z"}
                e2["evidence"] = [{"id": "ev-999", "retrieved_at": "2026-01-02T00:00:00Z"}]
                two = store.save_profile(org, "run-2", e2, "2026-01-02T00:00:01Z")
                self.assertEqual(one["profile_sha256"], two["profile_sha256"])
                self.assertEqual(store.count_history(org), 1)
            finally:
                store.close()


class RunnerTests(unittest.TestCase):
    def test_input_parser_preserves_bad_batch_members(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "in.json"
            path.write_text(json.dumps([{"organisation_number": synthetic_org_numbers(1)[0]}, {"name": "missing ID"}, None]), encoding="utf-8")
            values = read_organisation_input(path)
            self.assertEqual(len(values), 3)
            self.assertEqual(values[0], synthetic_org_numbers(1)[0])
            self.assertTrue(values[1])

    def test_one_envelope_per_input_and_six_state_contract(self):
        with tempfile.TemporaryDirectory() as d:
            records = synthetic_registry_records(3)
            numbers = list(records.keys())
            values = [numbers[0], numbers[1], "123", numbers[0]]
            store = StateStore(Path(d) / "db.sqlite3", Path(d) / "snaps")
            try:
                budget = BudgetManager(max_requests=20, max_runtime_seconds=60, max_cost_usd=1)
                runner = ResearchRunner(store=store, budget=budget, run_id="unit", skip_web=True)
                outputs, report = runner.run_batch(values, fixture_records=records)
                self.assertEqual(len(outputs), len(values))
                self.assertEqual(outputs[0]["organisation_number"], outputs[3]["organisation_number"])
                allowed = {"available", "not_available", "blocked", "not_applicable", "ambiguous", "failed"}
                for output in outputs:
                    self.assertIn(output["run"]["terminal_status"], {"completed", "failed"})
                    self.assertTrue(isinstance(output["claims"], list))
                    self.assertTrue(any(c["field"] == "summary_profile" for c in output["claims"]))
                    for claim in output["claims"]:
                        self.assertIn(claim["availability"], allowed)
                    evidence_ids = {ev["id"] for ev in output["evidence"]}
                    for ev in output["evidence"]:
                        self.assertTrue(ev["source_url"].startswith("https://"))
                        self.assertTrue(ev["retrieved_at"])
                        self.assertRegex(ev["content_sha256"], r"^[a-f0-9]{64}$")
                        self.assertTrue(ev["claim_span"])
                    for claim in output["claims"]:
                        self.assertTrue(set(claim["evidence_ids"]).issubset(evidence_ids))
                self.assertEqual(report["output_count"], len(values))
                self.assertEqual(report["invalid_input_count"], 1)
                self.assertEqual(report["operations"]["requests"], 0)
            finally:
                store.close()

    def test_envelopes_validate_against_provisional_json_schema(self):
        try:
            from jsonschema import Draft202012Validator, FormatChecker
        except ImportError:
            self.skipTest("install requirements-dev.txt to run JSON Schema validation")
        schema = json.loads((Path(__file__).resolve().parents[1] / "schema" / "output-envelope.schema.json").read_text(encoding="utf-8"))
        ids = synthetic_org_numbers(1)
        with tempfile.TemporaryDirectory() as d:
            store = StateStore(Path(d) / "db.sqlite3", Path(d) / "snaps")
            try:
                runner = ResearchRunner(store=store, budget=BudgetManager(max_requests=10, max_runtime_seconds=30, max_cost_usd=0.1),
                                        run_id="schema", skip_web=True)
                outputs, _ = runner.run_batch(ids, fixture_records=synthetic_registry_records(1))
                validator = Draft202012Validator(schema, format_checker=FormatChecker())
                errors = [error.message for error in validator.iter_errors(outputs[0])]
                self.assertEqual(errors, [])
            finally:
                store.close()

    def test_diff_only_emits_added_changed_not_unproven_removed(self):
        old = {"claims": [
            {"field": "name", "value": "A", "availability": "available", "evidence_ids": ["old"]},
            {"field": "address", "value": "old address", "availability": "available", "evidence_ids": ["addr-old"]},
        ]}
        new = [
            {"field": "name", "value": "B", "availability": "available", "evidence_ids": ["new"]},
            {"field": "address", "value": None, "availability": "blocked", "evidence_ids": []},
        ]
        changes = ResearchRunner._diff(old, new)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["type"], "changed")
        self.assertEqual(changes[0]["field"], "name")

    def test_unseen_company_is_not_silently_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            ids = synthetic_org_numbers(2)
            store = StateStore(Path(d) / "db.sqlite3", Path(d) / "snaps")
            try:
                runner = ResearchRunner(store=store, budget=BudgetManager(max_requests=0, max_runtime_seconds=20, max_cost_usd=0),
                                        run_id="empty", skip_web=True)
                outputs, _ = runner.run_batch(ids, fixture_records={ids[0]: synthetic_registry_records(1)[ids[0]]})
                self.assertEqual(len(outputs), 2)
                self.assertEqual(outputs[0]["run"]["terminal_status"], "completed")
                self.assertEqual(outputs[1]["run"]["terminal_status"], "completed")
                self.assertTrue(any(c["availability"] in {"not_available", "failed"} for c in outputs[1]["claims"]))
            finally:
                store.close()


class SocialAndReportTests(unittest.TestCase):
    def test_normalize_social_url(self):
        from signalpost.website import normalize_social_url
        self.assertEqual(normalize_social_url("https://www.linkedin.com/company/equinor"),
                         {"platform": "linkedin", "url": "https://linkedin.com/company/equinor"})
        self.assertIsNone(normalize_social_url("https://www.linkedin.com/in/someperson"))
        self.assertEqual(normalize_social_url("https://facebook.com/equinor"),
                         {"platform": "facebook", "url": "https://facebook.com/equinor"})
        self.assertIsNone(normalize_social_url("https://facebook.com/sharer/sharer.php?u=foo"))
        self.assertEqual(normalize_social_url("https://x.com/equinor"),
                         {"platform": "x", "url": "https://x.com/equinor"})
        self.assertIsNone(normalize_social_url("https://x.com/intent/tweet"))
        self.assertEqual(normalize_social_url("https://youtube.com/@Equinor"),
                         {"platform": "youtube", "url": "https://youtube.com/@Equinor"})

    def test_extract_page_facts_social_and_contact(self):
        from signalpost.website import extract_page_facts
        raw = b"""<!DOCTYPE html>
        <html>
        <head>
          <title>Test Company AS</title>
          <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "Organization",
            "name": "Test Company AS",
            "sameAs": ["https://www.linkedin.com/company/test-company", "https://twitter.com/testcompany"]
          }
          </script>
        </head>
        <body>
          <a href="mailto:contact@testcompany.no">Email us</a>
          <a href="tel:+4722334455">Call us</a>
          <a href="https://facebook.com/testcompany">Facebook</a>
        </body>
        </html>"""
        facts = extract_page_facts(raw, "https://testcompany.no/")
        self.assertIn("contact@testcompany.no", facts["emails"])
        self.assertIn("+4722334455", facts["phones"])
        platforms = {item["platform"] for item in facts["social_links"]}
        self.assertIn("linkedin", platforms)
        self.assertIn("x", platforms)
        self.assertIn("facebook", platforms)

    def test_report_generation(self):
        from signalpost.report import generate_html_report, generate_markdown_report
        outputs = [{
            "organisation_number": "999999999",
            "summary": "Test company summary [ev-1].",
            "identity_anchor": {"official_name": "Test AS", "organisation_number": "999999999"},
            "run": {"run_id": "r1", "started_at": "2026-01-01T00:00:00Z", "completed_at": "2026-01-01T00:00:01Z", "terminal_status": "completed"},
            "claims": [{"field": "official_name", "value": "Test AS", "availability": "available", "confidence": 1.0, "evidence_ids": ["ev-1"]}],
            "evidence": [{"id": "ev-1", "source_url": "https://example.org", "source_class": "test", "retrieved_at": "2026-01-01T00:00:00Z", "content_sha256": "a"*64, "claim_span": "test"}],
            "operations": {"requests": 1, "runtime_ms": 10, "third_party_cost_usd": 0.0},
        }]
        rep = {"run_id": "r1", "operations": {"requests": 1, "third_party_cost_usd": 0.0}, "terminal_status_counts": {"completed": 1}}
        html_doc = generate_html_report(outputs, rep)
        self.assertIn("<!DOCTYPE html>", html_doc)
        self.assertIn("Test AS", html_doc)
        md_doc = generate_markdown_report(outputs, rep)
        self.assertIn("# Signalpost Company Research Batch Report", md_doc)
        self.assertIn("Test AS", md_doc)


class SignalpostValidationPolicyTests(unittest.TestCase):
    def test_subsidiary_listing_group_website_suppresses_group_social_news_and_jobs(self):
        from signalpost.envelope import EnvelopeBuilder
        from signalpost.registry import normalize_registry_record
        from signalpost.website import PageParser, extract_page_facts
        from signalpost.runner import ResearchRunner
        from signalpost.store import StateStore
        from signalpost.budget import BudgetManager

        # Subsidiary record with group flag and parent organisation number
        rec = normalize_registry_record({
            "organisasjonsnummer": "915637353",
            "navn": "SKS PRODUKSJON AS",
            "erIKonsern": True,
            "overordnetEnhet": "987654321",
            "hjemmeside": "https://sks.no",
        })

        # The parent group site HTML
        html_content = b"""<!DOCTYPE html>
        <html>
        <head>
          <title>Konsern - SKS - Forside</title>
          <meta name="description" content="SKS is an energy group with multiple operating subsidiaries.">
        </head>
        <body>
          <h1>SKS Group Portal</h1>
          <a href="https://linkedin.com/company/sks-group">SKS Group LinkedIn</a>
          <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "JobPosting",
            "title": "Group Operations Director",
            "datePosted": "2026-03-01",
            "hiringOrganization": {"name": "SKS Konsern"}
          }
          </script>
          <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "NewsArticle",
            "headline": "SKS Group Annual Strategy",
            "datePublished": "2026-03-10"
          }
          </script>
        </body>
        </html>"""

        with tempfile.TemporaryDirectory() as d:
            store = StateStore(Path(d) / "db.sqlite3", Path(d) / "snaps")
            try:
                runner = ResearchRunner(store=store, budget=BudgetManager(max_requests=10, max_runtime_seconds=30, max_cost_usd=0.1),
                                        run_id="test-sub", skip_web=True)
                builder = EnvelopeBuilder("915637353", "test-sub", "2026-01-01T00:00:00Z")
                parser = PageParser("https://sks.no/")
                parser.feed(html_content.decode("utf-8"))
                facts = extract_page_facts(html_content, "https://sks.no/")

                site_res = {
                    "orgnr": "915637353",
                    "state": "ambiguous",
                    "reason": "registered website belongs to parent group or holding company, not the subsidiary's own site",
                    "confidence": 0.35,
                    "url": "https://sks.no/",
                    "response": FetchResult("https://sks.no/", "https://sks.no/", status=200, headers={"content-type": "text/html"}, body=html_content, retrieved_at="2026-01-01T00:00:00Z"),
                    "facts": facts,
                    "is_group_site": True,
                    "is_subsidiary": True,
                }

                runner._apply_site_result(builder, site_res, rec, [])

                # Verify website_content was NOT published as verified company-owned site
                avail_claims = {c["field"]: c for c in builder.claims if c.get("availability") == "available"}
                self.assertNotIn("website_content", avail_claims)
                self.assertNotIn("company_social_profiles", avail_claims)
                self.assertFalse(any(k.startswith("social_profile.") for k in avail_claims))
                self.assertFalse(any(k.startswith("job_posting.") for k in avail_claims))
                self.assertFalse(any(k.startswith("website_article.") for k in avail_claims))

                # Identity match records ambiguity with parent group note
                match_claim = next(c for c in builder.claims if c["field"] == "website_identity_match")
                self.assertEqual(match_claim["availability"], "ambiguous")
                self.assertIn("parent group", match_claim["notes"].lower())
            finally:
                store.close()

    def test_social_account_belonging_to_company(self):
        from signalpost.website import validate_social_account

        # 1. Web agencies in footer must be rejected
        ok, reason = validate_social_account("Fjord Seafood AS", "123456789", {"platform": "facebook", "url": "https://facebook.com/dekode"})
        self.assertFalse(ok)
        self.assertIn("web agency", reason.lower())

        ok, reason = validate_social_account("Fjord Seafood AS", "123456789", {"platform": "instagram", "url": "https://instagram.com/netlife_design"})
        self.assertFalse(ok)
        self.assertIn("web agency", reason.lower())

        # 2. Website theme and CMS must be rejected
        ok, reason = validate_social_account("Fjord Seafood AS", "123456789", {"platform": "x", "url": "https://x.com/wordpress"})
        self.assertFalse(ok)
        self.assertIn("cms", reason.lower())

        ok, reason = validate_social_account("Fjord Seafood AS", "123456789", {"platform": "github", "url": "https://github.com/elementor"})
        self.assertFalse(ok)
        self.assertIn("theme", reason.lower())

        # 3. Developer / footer link flag must be rejected
        ok, reason = validate_social_account("Fjord Seafood AS", "123456789", {"platform": "linkedin", "url": "https://linkedin.com/company/fjord-seafood"}, is_developer_link=True)
        self.assertFalse(ok)
        self.assertIn("designer, developer, or cms", reason.lower())

        # 4. Parent group social account must be rejected for subsidiary
        ok, reason = validate_social_account("Aon Norway AS", "123456789", {"platform": "linkedin", "url": "https://linkedin.com/company/aon"}, is_subsidiary=True, parent_name="Aon Group")
        self.assertFalse(ok)
        self.assertIn("parent group", reason.lower())

        # 5. Verified company social accounts must be accepted
        ok, reason = validate_social_account("Norsk Fiskeeksport AS", "123456789", {"platform": "linkedin", "url": "https://linkedin.com/company/norsk-fiskeeksport"})
        self.assertTrue(ok)

        ok, reason = validate_social_account("Aon Norway AS", "123456789", {"platform": "linkedin", "url": "https://linkedin.com/company/aon-norway"}, is_subsidiary=True, parent_name="Aon Group")
        self.assertTrue(ok)

    def test_news_items_must_be_real_dated_posts_not_menu_or_product_pages(self):
        from signalpost.website import is_valid_news_post

        # 1. Menu labels must be rejected
        self.assertFalse(is_valid_news_post({"title": "See more news", "datePublished": "2026-03-01"})[0])
        self.assertFalse(is_valid_news_post({"title": "Se flere nyheter", "datePublished": "2026-03-01"})[0])
        self.assertFalse(is_valid_news_post({"title": "Flere nyheter", "datePublished": "2026-03-01"})[0])
        self.assertFalse(is_valid_news_post({"title": "Alle nyheter", "datePublished": "2026-03-01"})[0])
        self.assertFalse(is_valid_news_post({"title": "Read more", "datePublished": "2026-03-01"})[0])
        self.assertFalse(is_valid_news_post({"title": "Nyheter", "datePublished": "2026-03-01"})[0])

        # 2. Product and service pages must be rejected
        self.assertFalse(is_valid_news_post({"title": "Offshore Sensor Pack", "datePublished": "2026-03-01", "url": "https://example.no/products/sensor-pack"})[0])
        self.assertFalse(is_valid_news_post({"title": "Subsea Maintenance Service", "datePublished": "2026-03-01", "@type": "Product"})[0])

        # 3. Modified or feed updated dates must be rejected as publish dates
        ok, reason = is_valid_news_post({"title": "Quarterly production results", "dateModified": "2026-04-01"})
        self.assertFalse(ok)
        self.assertIn("modified", reason.lower())

        ok, reason = is_valid_news_post({"title": "New solar energy contract", "updated": "2026-04-01"})
        self.assertFalse(ok)
        self.assertIn("modified", reason.lower())

        ok, reason = is_valid_news_post({"title": "Green energy transition report", "feed_updated": "2026-04-01"})
        self.assertFalse(ok)
        self.assertIn("modified", reason.lower())

        # 4. Real dated news post must be accepted
        ok, reason = is_valid_news_post({"title": "New offshore contract signed with Equinor", "datePublished": "2026-04-15", "url": "https://example.no/news/contract-equinor"})
        self.assertTrue(ok)

    def test_latest_accounts_is_newest_filing_fy2025_and_periods_same_length_older_to_newer(self):
        from signalpost.accounts import compare_financial_periods, extract_financial_values

        # 1. Column parser correctly assigns newest year to current amount and prior to prior_year_amount
        raw_text = "Sum driftsinntekter 1 500 000 1 200 000\nDriftsresultat 300 000 200 000\nÅrsresultat 250 000 150 000"
        vals = extract_financial_values(raw_text)
        self.assertEqual(vals["revenue"]["amount_as_printed"], "1 500 000")
        self.assertEqual(vals["revenue"]["prior_year_amount"], "1 200 000")

        # 2. Period comparison must reject periods of different lengths
        comp = compare_financial_periods("2024", "2025", {"revenue": {"amount_as_printed": "1 200 000"}},
                                         vals, older_period_months=6, newer_period_months=12)
        self.assertFalse(comp["comparable"])
        self.assertIn("different lengths", comp["reason"].lower())

        # 3. Period comparison for equal length must be ordered older to newer
        comp_equal = compare_financial_periods("2024", "2025",
                                               {"revenue": {"amount_as_printed": "1 200 000"}},
                                               {"revenue": {"amount_as_printed": "1 500 000"}},
                                               older_period_months=12, newer_period_months=12)
        self.assertTrue(comp_equal["comparable"])
        self.assertEqual(comp_equal["order"], "older_to_newer")
        metric = comp_equal["metrics"]["revenue"]
        self.assertEqual(metric["older_period"], "2024")
        self.assertEqual(metric["older_amount"], "1 200 000")
        self.assertEqual(metric["newer_period"], "2025")
        self.assertEqual(metric["newer_amount"], "1 500 000")
        self.assertEqual(metric["direction"], "older_to_newer")


if __name__ == "__main__":
    unittest.main()
