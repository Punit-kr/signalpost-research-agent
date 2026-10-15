from __future__ import annotations

import re
from typing import Any


def build_summary(orgnr: str, claims: list[dict[str, Any]], identity_anchor: dict[str, Any] | None,
                  changes: list[dict[str, Any]] | None = None) -> str:
    by_field: dict[str, list[dict[str, Any]]] = {}
    for c in claims:
        by_field.setdefault(c.get("field", ""), []).append(c)
    sentences = []

    # 1. Identity & Registration
    name_claim = next((x for x in by_field.get("official_name", []) if x.get("availability") == "available" and x.get("value")), None)
    if name_claim:
        sentences.append(f"{name_claim['value']} is the registered entity for Norwegian organisation number {orgnr} [{','.join(name_claim.get('evidence_ids', []))}].")
    else:
        sentences.append(f"No matching entity record was verified for organisation number {orgnr} in the sources checked.")

    form = next((x for x in by_field.get("legal_form", []) if x.get("availability") == "available" and x.get("value")), None)
    if form:
        val = form["value"]
        text = val.get("description") if isinstance(val, dict) else str(val)
        if text:
            sentences.append(f"The registered legal form is {text} [{','.join(form.get('evidence_ids', []))}].")

    # Address & location
    addr_claim = next((x for x in by_field.get("business_address", []) + by_field.get("postal_address", []) if x.get("availability") == "available" and x.get("value")), None)
    if addr_claim:
        addr = addr_claim["value"]
        mun = addr.get("kommune") if isinstance(addr, dict) else None
        pc = addr.get("postnummer") if isinstance(addr, dict) else None
        if mun or pc:
            sentences.append(f"The registered location is {mun or 'Norway'}{f' (postcode {pc})' if pc else ''} [{','.join(addr_claim.get('evidence_ids', []))}].")

    # Industry
    inds = next((x for x in by_field.get("industry_classifications", []) if x.get("availability") == "available" and x.get("value")), None)
    if inds:
        vals = inds["value"] if isinstance(inds["value"], list) else [inds["value"]]
        descriptions = [f"{v.get('code') + ' ' if v.get('code') else ''}{v.get('description')}" for v in vals if isinstance(v, dict) and (v.get("code") or v.get("description"))]
        if descriptions:
            sentences.append(f"The registry lists industry classification(s): {'; '.join(descriptions[:3])} [{','.join(inds.get('evidence_ids', []))}].")

    # 2. Operational description from website
    site = next((x for x in by_field.get("official_website", []) if x.get("availability") == "available" and x.get("value")), None)
    if site:
        sentences.append(f"The registry lists the official website as {site['value']} [{','.join(site.get('evidence_ids', []))}].")
    desc = next((x for x in by_field.get("website_description", []) if x.get("availability") == "available" and x.get("value")), None)
    if desc:
        sentences.append(f"The verified company website describes its activity as: {desc['value']} [{','.join(desc.get('evidence_ids', []))}].")

    # 3. Leadership & Employees
    leaders = [x for x in claims if x.get("field", "").startswith("leadership.") and x.get("availability") == "available" and x.get("value")]
    if leaders:
        leader_descs = []
        leader_eids = []
        for l in leaders[:3]:
            v = l.get("value", {})
            r_title = v.get("role", "")
            p_name = v.get("person_name") or (v.get("organisation", {}).get("name") if isinstance(v.get("organisation"), dict) else None)
            if p_name:
                leader_descs.append(f"{r_title}: {p_name}")
            leader_eids.extend(l.get("evidence_ids", []))
        if leader_descs:
            sentences.append(f"Registered leadership includes: {'; '.join(leader_descs)} [{','.join(dict.fromkeys(leader_eids))}].")

    employees = next((x for x in by_field.get("employees", []) if x.get("availability") == "available" and x.get("value") is not None), None)
    workplaces = next((x for x in by_field.get("registered_workplaces", []) if x.get("availability") == "available" and x.get("value")), None)
    if employees or workplaces:
        w_parts = []
        eids = []
        if employees and employees.get("value") is not None:
            w_parts.append(f"{employees['value']} registered employee(s)")
            eids.extend(employees.get("evidence_ids", []))
        if workplaces and isinstance(workplaces.get("value"), list):
            w_parts.append(f"{len(workplaces['value'])} registered workplace/sub-entity unit(s)")
            eids.extend(workplaces.get("evidence_ids", []))
        if w_parts:
            sentences.append(f"Workforce & operations: {', '.join(w_parts)} [{','.join(dict.fromkeys(eids))}].")

    # 4. Financials
    finances = [x for x in claims if x.get("field", "").startswith("financial.") and x.get("availability") == "available" and x.get("value")]
    if finances:
        periods = sorted(set(str(x.get("reporting_period")) for x in finances if x.get("reporting_period") and x.get("reporting_period") != "None"))
        fin_eids = dict.fromkeys(e for x in finances for e in x.get('evidence_ids', []))
        sentences.append(f"Financial figures were extracted conservatively from official annual-account documents for reporting period(s) {', '.join(periods)} [{','.join(fin_eids)}].")

    # 5. Hiring, social media & announcements
    jobs = [x for x in claims if x.get("field", "").startswith("job_posting.") and x.get("availability") == "available"]
    if jobs:
        job_eids = dict.fromkeys(e for x in jobs for e in x.get('evidence_ids', []))
        sentences.append(f"The company website published {len(jobs)} active job posting record(s) [{','.join(job_eids)}].")

    socials = [x for x in claims if x.get("field", "").startswith("social_profile.") and x.get("availability") == "available"]
    if socials:
        plat_names = sorted(set(x["field"].split(".")[1].title() for x in socials))
        soc_eids = dict.fromkeys(e for x in socials for e in x.get('evidence_ids', []))
        sentences.append(f"Verified company-controlled presence was confirmed on: {', '.join(plat_names)} [{','.join(soc_eids)}].")

    articles = [x for x in claims if x.get("field", "").startswith("website_article.") and x.get("availability") == "available"]
    if articles:
        art_eids = dict.fromkeys(e for x in articles for e in x.get('evidence_ids', []))
        sentences.append(f"The official website exposed {len(articles)} dated article or announcement record(s) in the pages checked [{','.join(art_eids)}].")

    # 6. Changes
    if changes:
        added_count = sum(1 for c in changes if c.get("type") == "added")
        changed_count = sum(1 for c in changes if c.get("type") == "changed")
        if added_count or changed_count:
            sentences.append(f"Change detection vs prior profile: {added_count} field(s) added, {changed_count} field(s) modified.")

    # 7. Unknowns & Ambiguities
    unknown = []
    if not leaders:
        unknown.append("registered leadership/roles")
    if not finances:
        unknown.append("extractable financial figures")
    if not articles:
        unknown.append("dated company announcements")
    if not site:
        unknown.append("official website")
    if unknown:
        sentences.append("Not verified in this run: " + ", ".join(unknown) + ".")

    return " ".join(sentences)

