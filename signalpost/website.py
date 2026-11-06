from __future__ import annotations

import html
import json
import re
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from .http_client import FetchResult, HTTPClient, safe_public_url

TRACKING_QUERY_KEYS = {"utm_source", "utm_medium", "utm_campaign", "gclid", "fbclid"}


def canonical_url(url: str, base: str | None = None) -> str | None:
    if base:
        url = urllib.parse.urljoin(base, url)
    try:
        p = urllib.parse.urlsplit(url.strip())
        if p.scheme.lower() not in {"http", "https"} or not p.hostname or p.username or p.password:
            return None
        # Upgrade HTTP links to HTTPS; do not make an insecure request.
        scheme = "https"
        host = p.hostname.lower()
        if p.port and p.port != 443:
            return None
        query = urllib.parse.urlencode([(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if k.lower() not in TRACKING_QUERY_KEYS])
        path = p.path or "/"
        clean = urllib.parse.urlunsplit((scheme, host, path, query, ""))
        ok, _ = safe_public_url(clean)
        return clean if ok else None
    except (ValueError, UnicodeError):
        return None


def hostname(url: str) -> str | None:
    try:
        host = urllib.parse.urlsplit(url).hostname
        if not host: return None
        host = host.lower()
        return host[4:] if host.startswith("www.") else host
    except ValueError: return None


SOCIAL_HOSTS = {
    "linkedin.com": "linkedin",
    "facebook.com": "facebook",
    "instagram.com": "instagram",
    "x.com": "x",
    "twitter.com": "x",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "tiktok.com": "tiktok",
    "github.com": "github",
}


def normalize_social_url(url: str) -> dict[str, str] | None:
    try:
        raw = url.strip()
        if not re.match(r"^https?://", raw, re.I):
            if any(raw.lower().startswith(d) for d in SOCIAL_HOSTS):
                raw = "https://" + raw
            else:
                return None
        parsed = urllib.parse.urlsplit(raw)
    except (ValueError, UnicodeError):
        return None
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    platform = next((label for domain, label in SOCIAL_HOSTS.items() if host == domain or host.endswith("." + domain)), None)
    if not platform:
        return None
    parts = [part.strip() for part in parsed.path.split("/") if part.strip()]
    lowered = [part.casefold() for part in parts]
    rejected_first = {
        "facebook": {"sharer", "sharer.php", "share.php", "dialog", "policy.php", "privacy", "events", "groups", "plugins"},
        "instagram": {"p", "reel", "reels", "stories", "explore"},
        "x": {"intent", "share", "home", "search", "i"},
        "github": {"features", "pricing", "security", "enterprise", "join", "login", "pulls", "issues"},
    }
    if not parts or lowered[0] in rejected_first.get(platform, set()):
        return None
    if platform == "facebook" and lowered[0] == "profile.php":
        return None
    if platform == "linkedin" and (lowered[0] != "company" or len(parts) < 2):
        return None
    if platform == "youtube" and lowered[0] not in {"channel", "user", "c"} and not parts[0].startswith("@"):
        return None
    if host == "youtu.be":
        return None
    if platform == "tiktok" and not parts[0].startswith("@"):
        return None
    if platform == "x" and len(parts) != 1:
        return None
    if platform == "github" and len(parts) != 1:
        return None
    canonical_host = {
        "linkedin": "linkedin.com",
        "facebook": "facebook.com",
        "instagram": "instagram.com",
        "x": "x.com",
        "youtube": "youtube.com",
        "tiktok": "tiktok.com",
        "github": "github.com",
    }[platform]
    if platform == "linkedin":
        parts = parts[:2]
    elif platform == "youtube":
        parts = parts[:1] if parts[0].startswith("@") else parts[:2]
    elif platform in {"x", "tiktok", "github"}:
        parts = parts[:1]
    return {"platform": platform, "url": f"https://{canonical_host}/{'/'.join(parts)}"}


DISALLOWED_SOCIAL_HANDLES = {
    # CMS, theme vendors, site builders
    "wordpress", "automattic", "wpengine", "elementor", "envato", "themeforest",
    "shopify", "squarespace", "wix", "webflow", "drupal", "joomla",
    "ghost", "bootstrap", "twbs", "woocommerce", "prestashop", "magento",
    "hubspot", "mailchimp", "klarna", "vipps", "stripe", "cloudflare", "google",
    # Agencies & web developers (Norwegian & international)
    "dekode", "netlife", "netlifedesign", "creuna", "knowit", "bouvet",
    "soprasteria", "tietoevry", "makingwaves", "boldscandinavia", "apriil",
    "try", "schjærven", "anti", "bleed", "mona", "goodmorning", "novanet",
    "webstep", "miles", "bekk", "dentsu", "publicis", "vaimo", "geta",
    "redink", "coretrek", "mediadigital", "gasta", "findexa", "eniro",
    "gulesider", "visma", "proisp", "domeneshop", "misshosting", "syse",
    "hostinger", "siteground",
    # Template & generic handles
    "username", "yourprofile", "yourpage", "company-name", "handle",
    "pagename", "profile", "example", "template", "theme", "admin", "channel",
    # Platform self-handles
    "facebook", "instagram", "linkedin", "twitter", "x", "youtube", "tiktok", "github",
}

MENU_AND_NAV_TITLES = {
    "see more news", "see more", "more news", "se flere nyheter", "se flere",
    "flere nyheter", "alle nyheter", "mer om oss", "les mer", "read more",
    "news", "nyheter", "aktuelt", "presse", "press", "pressemeldinger",
    "press releases", "arkiv", "archive", "siste nytt", "latest news",
    "view all", "view all news", "tilbake", "back to news", "back", "home",
    "forside", "meny", "menu", "search", "søk", "contact us", "kontakt oss",
    "contact", "kontakt", "about us", "om oss", "about", "om", "next",
    "neste", "previous", "forrige", "artikler", "articles", "blog", "blogg",
    "oversikt", "overview", "hva skjer", "whats new", "what's new",
}

PRODUCT_PATH_REGEX = re.compile(
    r"/(?:products?|produkter?|services?|tjenester?|solutions?|losninger?|shop|butikk|vare|varer|catalog|item|items)(?:/|$)",
    re.I
)

PARKED_MARKERS = (
    "domain is for sale", "domain for sale", "hugedomains", "parked at",
    "miss hosting", "her flytter snart en ny gjest", "has been informing visitors",
    "find the best information and most relevant links on all topics related to",
    "buy this domain", "denne nettsiden er parkert", "under construction",
)

GROUP_KEYWORDS = {
    "konsern", "group", "holdings", "holding", "portfolio", "konsernselskap",
    "datterselskap", "subsidiaries", "umbrella", "investeringsselskap"
}


def _tokens(value: Any) -> list[str]:
    import unicodedata
    legal_and_generic = {
        "as", "asa", "ans", "da", "enk", "iks", "sa", "sam", "sti", "stiftelsen",
        "nuf", "ab", "b", "v", "limited", "ltd", "inc", "plc", "the", "og", "and",
    }
    text = str(value or "").translate(str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    return [token for token in re.findall(r"[a-z0-9]+", text) if token not in legal_and_generic and len(token) > 1]


def validate_social_account(
    company_name: str | None,
    orgnr: str,
    social_item: dict[str, str],
    *,
    is_subsidiary: bool = False,
    parent_name: str | None = None,
    is_developer_link: bool = False,
) -> tuple[bool, str]:
    """Check that each social account really belongs to the company.

    Rejects links pointing to:
    - Web agency that built the site
    - Website theme / CMS pages
    - Parent group when publishing for a subsidiary
    - Generic placeholders or unrelated companies
    """
    if is_developer_link:
        return False, "Link is associated with website designer, developer, or CMS credits in footer"

    url = (social_item.get("url") or "").strip()
    platform = social_item.get("platform") or ""
    if not url:
        return False, "Missing social URL"

    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception:
        return False, "Malformed social URL"

    path_parts = [p for p in parsed.path.strip("/").split("/") if p]
    if not path_parts:
        return False, "Social URL lacks account path"

    # Extract account handle
    handle = path_parts[1] if (platform == "linkedin" and len(path_parts) >= 2) else path_parts[0]
    handle = handle.lstrip("@").strip()
    handle_clean = re.sub(r"[^a-z0-9]", "", handle.lower())

    if not handle_clean:
        return False, "Empty or invalid social handle"

    # 1. Block web agencies, theme developers, CMS platforms and template placeholders
    for blocked in DISALLOWED_SOCIAL_HANDLES:
        blocked_clean = re.sub(r"[^a-z0-9]", "", blocked.lower())
        if handle_clean == blocked_clean or blocked_clean in handle_clean:
            return False, f"Social account '{handle}' belongs to web agency, CMS, theme vendor, or placeholder"

    # 2. Block parent group social accounts when publishing for a subsidiary
    company_tokens = set(_tokens(company_name))
    if is_subsidiary:
        parent_tokens = set(_tokens(parent_name)) if parent_name else set()
        subsidiary_distinctive = company_tokens - parent_tokens - {"as", "asa", "holding", "konsern"}
        handle_tokens = set(_tokens(handle))

        # If handle matches the parent group name or only parent tokens without subsidiary qualifier
        if parent_tokens and handle_tokens.issubset(parent_tokens) and not (handle_tokens & subsidiary_distinctive):
            return False, f"Social account '{handle}' belongs to parent group '{parent_name}', not the subsidiary"
        # If subsidiary has an operational or geographic distinction but handle is just the parent brand
        if len(company_tokens) >= 2 and any(t in company_tokens for t in ("norway", "norge", "produksjon", "drift", "retail", "eiendom")):
            qualifiers = company_tokens & {"norway", "norge", "produksjon", "drift", "retail", "eiendom"}
            if not (handle_tokens & qualifiers):
                return False, f"Social account '{handle}' represents the parent group, lacking subsidiary qualifier {qualifiers}"

    # 3. Match against company name
    if not company_tokens:
        return False, "Cannot verify social account without registered company name"

    handle_tokens = set(_tokens(handle))
    overlap = company_tokens & handle_tokens
    compact_name = "".join(_tokens(company_name))

    if not overlap and compact_name not in handle_clean and handle_clean not in compact_name:
        return False, f"Social handle '{handle}' has no matching name tokens with company '{company_name}'"

    return True, "Social account verified as belonging to company"


def is_valid_news_post(article: dict[str, Any], page_url: str = "") -> tuple[bool, str]:
    """Check that news items are real, dated posts.

    Rejects:
    - Menu labels like "See more news" or generic navigation headers
    - Product or service pages
    - Items where date is merely "modified" or feed "updated" rather than a real publish date
    """
    title = str(article.get("headline") or article.get("name") or article.get("title") or "").strip()
    if not title:
        return False, "News item has no headline/title"

    norm_title = re.sub(r"\s+", " ", title.strip().lower())

    # 1. Reject menu labels and navigation text
    if norm_title in MENU_AND_NAV_TITLES:
        return False, f"Headline '{title}' is a menu label or navigation header, not a news post"

    if re.match(r"^(?:see\s+more|se\s+flere|alle\s+nyheter|flere\s+nyheter|read\s+more|les\s+mer|view\s+all)\b", norm_title):
        return False, f"Headline '{title}' is a navigation or menu label"

    words = norm_title.split()
    if len(words) < 3 and len(norm_title) <= 15:
        return False, f"Headline '{title}' is too short/generic to be an article"

    # 2. Reject product and service pages
    item_url = str(article.get("url") or page_url or "")
    if item_url and PRODUCT_PATH_REGEX.search(item_url):
        return False, f"URL path indicates a product/service page, not a news post: {item_url}"

    typ = str(article.get("@type", "")).lower()
    if any(t in typ for t in ("product", "service", "offer", "itemlist", "store")):
        return False, f"Schema type '{typ}' is a product/service, not an article"

    # 3. Reject modified or feed updated dates: must have genuine publish date
    has_explicit_publish = bool(article.get("datePublished") or article.get("pubDate") or article.get("dateCreated"))
    only_modified = not has_explicit_publish and bool(article.get("dateModified") or article.get("modified") or article.get("updated") or article.get("feed_updated") or article.get("lastModified") or article.get("sistEndret"))

    if only_modified:
        return False, "A 'modified' or feed 'updated' date is not a publish date"

    pub_date = str(article.get("datePublished") or article.get("pubDate") or article.get("dateCreated") or "")
    if not pub_date:
        return False, "News item lacks a genuine publication date"

    # Validate date structure
    m = re.search(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", pub_date)
    if not m:
        if not re.match(r"^20\d{2}", pub_date):
            return False, f"Publication date '{pub_date}' is not a valid dated timestamp"
        year = int(pub_date[:4])
    else:
        year = int(m.group(1))

    if year < 2000 or year > 2026:
        return False, f"Publication date year {year} is out of realistic range"

    return True, "Valid dated news post"


class PageParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title = ""
        self.meta: dict[str, str] = {}
        self.headings: list[str] = []
        self.links: list[dict[str, str]] = []
        self.times: list[dict[str, str]] = []
        self.jsonld_texts: list[str] = []
        self.emails: set[str] = set()
        self.phones: set[str] = set()
        self.social_candidates: list[dict[str, Any]] = []
        self._title_depth = 0
        self._heading_depth = 0
        self._script_type = ""
        self._script_buf: list[str] = []
        self._anchor: dict[str, Any] | None = None
        self._anchor_depth = 0
        self._heading_buf: list[str] = []
        self._title_buf: list[str] = []
        self._in_footer = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        tag_low = tag.lower()
        if tag_low == "footer" or any("footer" in (a.get(k) or "").lower() for k in ("class", "id", "role")):
            self._in_footer += 1
        elif tag_low == "meta":
            key = (a.get("name") or a.get("property") or a.get("itemprop") or "").lower()
            val = a.get("content", "").strip()
            if key and val: self.meta[key] = val[:1000]
        elif tag_low == "title":
            self._title_depth = 1; self._title_buf = []
        elif tag_low in {"h1", "h2", "h3"}:
            self._heading_depth = 1; self._heading_buf = []
        elif tag_low == "a" and a.get("href"):
            raw_href = a["href"].strip()
            if raw_href.lower().startswith("mailto:"):
                email = raw_href[7:].split("?")[0].strip()
                if "@" in email and "." in email and len(email) < 100:
                    self.emails.add(email)
            elif raw_href.lower().startswith("tel:"):
                phone = raw_href[4:].split("?")[0].strip()
                if len(phone) >= 5 and len(phone) < 30:
                    self.phones.add(phone)
            soc = normalize_social_url(raw_href)
            if soc:
                is_dev = any(dev in (a.get("rel") or "").lower() or dev in (a.get("class") or "").lower() for dev in ("designer", "developer", "author", "creator", "credits"))
                soc_entry = {**soc, "in_footer": bool(self._in_footer > 0), "is_developer_link": bool(is_dev or self._in_footer > 0)}
                if not any(s.get("url") == soc["url"] for s in self.social_candidates):
                    self.social_candidates.append(soc_entry)
            target = canonical_url(raw_href, self.base_url)
            if target:
                self._anchor = {"url": target, "text": "", "rel": a.get("rel", ""), "type": a.get("type", ""), "in_footer": bool(self._in_footer > 0)}
                self._anchor_depth = 1
        elif tag_low == "time":
            val = a.get("datetime") or a.get("content")
            if val: self.times.append({"datetime": val, "text": ""})
        elif tag_low == "script" and a.get("type", "").lower().split(";")[0] == "application/ld+json":
            self._script_type = "jsonld"; self._script_buf = []

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "footer":
            self._in_footer = max(0, self._in_footer - 1)
        elif t == "title" and self._title_depth:
            self.title = " ".join("".join(self._title_buf).split())[:500]; self._title_depth = 0
        elif t in {"h1", "h2", "h3"} and self._heading_depth:
            val = " ".join("".join(self._heading_buf).split())
            if val and val not in self.headings: self.headings.append(val[:300])
            self._heading_depth = 0
        elif t == "a" and self._anchor is not None:
            self._anchor["text"] = " ".join(html.unescape(self._anchor.get("text", "")).split())[:400]
            if self._anchor["text"]: self.links.append(self._anchor)
            self._anchor = None; self._anchor_depth = 0
        elif t == "script" and self._script_type == "jsonld":
            val = "".join(self._script_buf).strip()
            if val: self.jsonld_texts.append(val[:500_000])
            self._script_type = ""; self._script_buf = []

    def handle_data(self, data: str) -> None:
        if self._title_depth: self._title_buf.append(data)
        if self._heading_depth: self._heading_buf.append(data)
        if self._anchor is not None: self._anchor["text"] += " " + data
        if self._script_type == "jsonld": self._script_buf.append(data)


def extract_jsonld(parser: PageParser) -> list[Any]:
    out = []
    for raw in parser.jsonld_texts:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list): out.extend(parsed)
            elif isinstance(parsed, dict):
                graph = parsed.get("@graph")
                out.extend(graph if isinstance(graph, list) else [parsed])
        except (json.JSONDecodeError, RecursionError):
            continue
    return out


def _norm_name(value: str | None) -> str:
    if not value: return ""
    value = value.casefold()
    value = re.sub(r"\b(as|asa|ab|da|nuf|ks|enk|sa|ba|se|oy|ltd|limited|inc|gmbh)\b", " ", value)
    return re.sub(r"[^a-z0-9æøå]+", " ", value).strip()


def match_company_website(
    company_name: str | None,
    orgnr: str,
    raw: bytes,
    parser: PageParser,
    *,
    is_subsidiary: bool = False,
    parent_name: str | None = None,
) -> tuple[str, float, str]:
    """Check that the website published is the company's own, not a parent group's or a brand's site."""
    text = raw.decode("utf-8", errors="ignore")[:500_000]
    low_text = text.lower()

    # 1. Parked or placeholder domain detection
    for marker in PARKED_MARKERS:
        if marker in low_text:
            return "ambiguous", 0.1, f"captured page is a parked, for-sale, or placeholder domain: '{marker}'"

    site_names = []
    for ld in extract_jsonld(parser):
        if not isinstance(ld, dict): continue
        typ = ld.get("@type", "")
        if isinstance(typ, list): typ = " ".join(typ)
        if any(t in str(typ).lower() for t in ("organization", "corporation", "localbusiness", "professionalservice")):
            candidate = ld.get("name")
            if candidate: site_names.append(str(candidate))
    site_names.extend([parser.meta.get("og:site_name", ""), parser.title, *(parser.headings[:4])])
    site_identity_text = " ".join(str(s) for s in site_names if s).lower()

    # 2. Parent group / umbrella / holding company detection
    has_group_marker = any(re.search(rf"\b{re.escape(k)}\b", site_identity_text) for k in GROUP_KEYWORDS)
    comp_tokens = set(_tokens(company_name))
    is_sports_club = bool(re.search(r"(?:^|\s)B\.?\s*I\.?\s*L\.?(?:\s|$)", str(company_name or ""), re.I))

    # Business sports club pointing to parent company operating site
    if is_sports_club and "bedriftsidrett" not in low_text and "b i l" not in low_text:
        return "ambiguous", 0.3, "business sports-club entity points to the operating company's site without club evidence"

    # If the page identity represents a parent group/konsern and the entity is a subsidiary
    if has_group_marker and is_subsidiary:
        return "ambiguous", 0.35, "registered website belongs to parent group or holding company, not the subsidiary's own site"

    # 3. Brand site without company's legal name match
    # When distinctive tokens of the legal name do not appear in homepage identity
    if comp_tokens and len(comp_tokens) >= 2:
        homepage_tokens = set(_tokens(site_identity_text))
        overlap = comp_tokens & homepage_tokens
        if not overlap:
            return "ambiguous", 0.35, "registered website belongs to a brand or parent company without matching legal name"

    # 4. Organisation number verification
    compact = re.sub(r"\D", "", text)
    if orgnr in compact:
        # If the page represents a group/umbrella, an organisation number in contact list is not company's own site
        if has_group_marker and is_subsidiary:
            return "ambiguous", 0.4, "organisation number listed on parent group site; website belongs to parent group, not the subsidiary"
        return "verified", 0.995, "exact organisation number found on official site"

    # 5. Exact company name token match
    name = _norm_name(company_name)
    for candidate in site_names:
        cand = _norm_name(candidate)
        if name and cand and name == cand:
            return "verified", 0.97, f"exact company name match in page metadata/title: {candidate[:120]}"
        nt, ct = set(name.split()), set(cand.split())
        if len(nt) >= 2 and nt == ct:
            return "verified", 0.92, f"exact company-name token match: {candidate[:120]}"

    return "ambiguous", 0.35, "registered website did not expose a strong name or organisation-number match"


def pick_followup_links(links: list[dict[str, str]], root_url: str, *, limit: int = 8) -> list[dict[str, str]]:
    root_host = hostname(root_url)
    weights = [
        (re.compile(r"career|careers|job|jobs|stilling|ledige|jobb|karriere", re.I), 100, "jobs"),
        (re.compile(r"about|om-oss|om-virksomheten|company|who-we-are|om-foretaket", re.I), 90, "about"),
        (re.compile(r"leadership|management|ledelse|styret|board|ansatte|team", re.I), 80, "leadership"),
        (re.compile(r"news|nyheter|press|media|investor|pressemelding|aktuelt", re.I), 75, "news"),
        (re.compile(r"product|service|tjeneste|produkt|solution|losning", re.I), 65, "products"),
        (re.compile(r"contact|kontakt", re.I), 60, "contact"),
        (re.compile(r"annual-report|annualreport|arsrapport|årsrapport|investor", re.I), 55, "reports"),
    ]
    scored = []
    seen = set()
    for link in links:
        url = link["url"]
        if hostname(url) != root_host or url == root_url or url in seen: continue
        seen.add(url)
        hay = url + " " + link.get("text", "")
        for rx, score, kind in weights:
            if rx.search(hay):
                scored.append((score, url, link.get("text", ""), kind)); break
    scored.sort(key=lambda x: (-x[0], x[1]))
    out = []
    kinds = set()
    for score, url, text, kind in scored:
        if kind in kinds and len(out) > 3: continue
        out.append({"url": url, "anchor_text": text, "kind": kind, "priority": score})
        kinds.add(kind)
        if len(out) >= limit: break
    return out


def extract_page_facts(raw: bytes, url: str) -> dict[str, Any]:
    parser = PageParser(url)
    try: parser.feed(raw.decode("utf-8", errors="replace"))
    except Exception: pass
    jsonld = extract_jsonld(parser)
    description = parser.meta.get("description") or parser.meta.get("og:description") or parser.meta.get("twitter:description")
    canonical = canonical_url(parser.meta.get("og:url", ""), url) if parser.meta.get("og:url") else url
    orgs, jobs, other = [], [], []
    socials: dict[tuple[str, str], dict[str, str]] = {}
    for soc in parser.social_candidates:
        socials[(soc["platform"], soc["url"])] = soc
    for obj in jsonld:
        if not isinstance(obj, dict): continue
        typ = obj.get("@type", "")
        types = set(str(x).lower() for x in typ) if isinstance(typ, list) else {str(typ).lower()}
        if types & {"organization", "corporation", "localbusiness", "professionalservice"}:
            orgs.append(obj)
            same_as = obj.get("sameAs")
            urls = same_as if isinstance(same_as, list) else ([same_as] if same_as else [])
            for raw_u in urls:
                if isinstance(raw_u, str):
                    soc = normalize_social_url(raw_u)
                    if soc: socials[(soc["platform"], soc["url"])] = soc
        if "jobposting" in types:
            jobs.append(obj)
        if "article" in types or "newsarticle" in types or "pressrelease" in types:
            other.append(obj)
    sorted_socials = sorted(socials.values(), key=lambda x: (x["platform"], x["url"]))
    return {"parser": parser, "title": parser.title or None, "description": description,
            "headings": parser.headings[:12], "links": parser.links, "jsonld": jsonld,
            "organization_jsonld": orgs, "job_postings": jobs, "articles": other,
            "canonical_url": canonical, "times": parser.times,
            "social_links": sorted_socials, "emails": sorted(parser.emails), "phones": sorted(parser.phones)}


class RobotsPolicy:
    def __init__(self, client: HTTPClient, store=None):
        self.client, self.store = client, store
        self._cache: dict[str, tuple[bool, str, int | None]] = {}

    def may_fetch(self, target_url: str, *, orgnr: str) -> tuple[bool, str, int | None]:
        p = urllib.parse.urlsplit(target_url)
        origin = f"https://{p.netloc}"
        if origin in self._cache: return self._cache[origin]
        robots_url = origin + "/robots.txt"
        cached = self.store.cache_get(robots_url, 24 * 3600) if self.store else None
        if cached:
            raw, status, headers = cached["body"], cached["status"], cached["headers"]
            self.client.budget.record_cache_hit(orgnr=orgnr)
        else:
            response = self.client.fetch(robots_url, orgnr=orgnr, max_bytes=512_000)
            raw, status, headers = response.body, response.status, response.headers
            if self.store and response.status is not None and response.error is None:
                self.store.cache_put(robots_url, status, headers, raw, __import__("hashlib").sha256(raw).hexdigest())
        if status in (401, 403, 429):
            decision = (False, f"robots policy fetch blocked with HTTP {status}", status)
        elif status == 404:
            decision = (True, "robots.txt not found; no disallow rules published", status)
        elif status is None or (status >= 500):
            decision = (False, "robots policy could not be checked; crawling skipped", status)
        elif status and 200 <= status < 300:
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(robots_url)
            parser.parse(raw.decode("utf-8", errors="replace").splitlines())
            path = urllib.parse.urlsplit(target_url).path or "/"
            allowed = parser.can_fetch("SignalpostCompanyResearchAgent", target_url)
            decision = (allowed, "robots.txt permits URL" if allowed else f"robots.txt disallows {path}", status)
        else:
            decision = (False, f"robots status {status}; crawling skipped conservatively", status)
        self._cache[origin] = decision
        return decision
