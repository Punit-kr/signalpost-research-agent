from __future__ import annotations

import ipaddress
import json
import datetime as dt
import random
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Mapping

from .budget import BudgetExceeded, BudgetManager

USER_AGENT = "SignalpostCompanyResearchAgent/0.1 (+https://builderr.ai/challenges/signalpost)"

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

@dataclass
class FetchResult:
    requested_url: str
    final_url: str
    status: int | None
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    error: str | None = None
    attempts: int = 0
    latency_ms: int = 0
    cache_hit: bool = False
    retrieved_at: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300 and self.error is None


def safe_public_url(url: str) -> tuple[bool, str]:
    """Restrict crawling to public HTTP(S), with HTTPS required and local targets rejected."""
    try:
        p = urllib.parse.urlsplit(url)
        if p.scheme.lower() != "https":
            return False, "only HTTPS URLs are permitted"
        if not p.hostname:
            return False, "URL has no hostname"
        if p.username or p.password:
            return False, "URL userinfo is not permitted"
        if p.port not in (None, 443):
            return False, "non-standard ports are not permitted"
        host = p.hostname.rstrip(".").lower()
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".test", ".invalid")):
            return False, "local or reserved host is not permitted"
        try:
            ip = ipaddress.ip_address(host)
            if not ip.is_global:
                return False, "non-public IP address is not permitted"
        except ValueError:
            # DNS rebinding is addressed by never accepting redirects to non-HTTPS and by restricting schemes/ports.
            if "." not in host:
                return False, "single-label host is not permitted"
        return True, host
    except ValueError:
        return False, "invalid URL"


class HTTPClient:
    def __init__(self, budget: BudgetManager, *, timeout: float = 12.0, retries: int = 2, max_redirects: int = 3):
        self.budget = budget
        self.timeout = timeout
        self.retries = retries
        self.max_redirects = max_redirects
        self._opener = urllib.request.build_opener(_NoRedirect())

    def fetch(self, url: str, *, orgnr: str | None = None, max_bytes: int = 2_000_000, reserved_requests: int = 0) -> FetchResult:
        start = time.monotonic()
        ok_url, host_or_error = safe_public_url(url)
        if not ok_url:
            return FetchResult(url, url, None, error=f"unsafe_url:{host_or_error}")
        current = url
        total_attempts = 0
        redirects = 0
        last: FetchResult | None = None
        while True:
            try:
                self.budget.check_time()
                ok_url, host_or_error = safe_public_url(current)
                if not ok_url:
                    return FetchResult(url, current, last.status if last else None, error=f"unsafe_redirect:{host_or_error}", attempts=total_attempts, latency_ms=int((time.monotonic()-start)*1000))
                req = urllib.request.Request(current, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json,text/plain,application/pdf,*/*;q=0.5"}, method="GET")
                self.budget.consume_request(host=host_or_error, orgnr=orgnr, reserved=reserved_requests)
                total_attempts += 1
                req_start = time.monotonic()
                try:
                    response = self._opener.open(req, timeout=self.timeout)
                    status = getattr(response, "status", 200)
                    headers = {k.lower(): v for k, v in response.headers.items()}
                    body = response.read(max_bytes + 1)
                    response.close()
                except urllib.error.HTTPError as exc:
                    status = exc.code
                    headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
                    body = exc.read(min(max_bytes, 8192)) if exc.fp else b""
                    exc.close()
                latency = (time.monotonic() - req_start) * 1000
                if len(body) > max_bytes:
                    body = body[:max_bytes]
                    result = FetchResult(url, current, status, headers, body, "response_too_large", total_attempts, int((time.monotonic()-start)*1000), False, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
                    self.budget.record_response(status=status, latency_ms=latency, bytes_received=len(body), error=result.error, orgnr=orgnr)
                    return result
                self.budget.record_response(status=status, latency_ms=latency, bytes_received=len(body), orgnr=orgnr)
                last = FetchResult(url, current, status, headers, body, None, total_attempts, int((time.monotonic()-start)*1000), False, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
                if status in (301, 302, 303, 307, 308):
                    location = headers.get("location")
                    if not location:
                        return FetchResult(url, current, status, headers, body, "redirect_without_location", total_attempts, int((time.monotonic()-start)*1000))
                    if redirects >= self.max_redirects:
                        return FetchResult(url, current, status, headers, body, "too_many_redirects", total_attempts, int((time.monotonic()-start)*1000))
                    current = urllib.parse.urljoin(current, location)
                    redirects += 1
                    # Every redirect target is another request and counts against the budget.
                    continue
                if status == 429 or status >= 500:
                    if total_attempts <= self.retries:
                        self.budget.record_retry(orgnr=orgnr)
                        retry_after = headers.get("retry-after", "")
                        try:
                            delay = min(2.0, float(retry_after)) if retry_after else min(1.5, (0.2 * (2 ** (total_attempts - 1)) + random.random() * 0.1))
                        except ValueError:
                            delay = 0.3
                        time.sleep(delay)
                        continue
                if status < 200 or status >= 300:
                    err = "blocked" if status in (401, 403, 407, 429) else ("not_found" if status == 404 else f"http_{status}")
                    last.error = err
                return last
            except BudgetExceeded:
                raise
            except (urllib.error.URLError, TimeoutError, socket.timeout, OSError, ValueError) as exc:
                self.budget.record_response(status=None, latency_ms=(time.monotonic()-start)*1000, error=str(exc), orgnr=orgnr)
                last = FetchResult(url, current, None, error=f"network_error:{type(exc).__name__}", attempts=total_attempts, latency_ms=int((time.monotonic()-start)*1000), retrieved_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
                if total_attempts <= self.retries:
                    self.budget.record_retry(orgnr=orgnr)
                    time.sleep(min(0.6, 0.15 * total_attempts))
                    continue
                return last
