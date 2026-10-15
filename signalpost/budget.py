from __future__ import annotations

import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class BudgetManager:
    max_requests: int = 2000
    max_runtime_seconds: int = 2700
    max_cost_usd: float = 10.0
    max_requests_per_domain: int = 12
    max_requests_per_registry_domain: int = 1900
    started_monotonic: float = field(default_factory=time.monotonic)
    started_at_epoch: float = field(default_factory=time.time)
    requests: int = 0
    cost_usd: float = 0.0
    cache_hits: int = 0
    retries: int = 0
    failures: int = 0
    blocked: int = 0
    status_counts: Counter = field(default_factory=Counter)
    host_counts: Counter = field(default_factory=Counter)
    per_org_requests: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    per_org_latency_ms: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    per_org_cache_hits: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    per_org_retries: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    latency_ms: list[float] = field(default_factory=list)
    bytes_received: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def check_time(self) -> None:
        if time.monotonic() - self.started_monotonic >= self.max_runtime_seconds:
            raise BudgetExceeded("run time budget exhausted")

    def consume_request(self, *, host: str, orgnr: str | None = None, reserved: int = 0) -> None:
        with self._lock:
            if time.monotonic() - self.started_monotonic >= self.max_runtime_seconds:
                raise BudgetExceeded("run time budget exhausted")
            if self.requests + 1 + max(0, reserved) > self.max_requests:
                raise BudgetExceeded("outbound request budget exhausted")
            domain_cap = self.max_requests_per_registry_domain if host == "brreg.no" or host.endswith(".brreg.no") else self.max_requests_per_domain
            if self.host_counts[host] + 1 > domain_cap:
                raise BudgetExceeded(f"per-domain request budget exhausted for {host}")
            self.requests += 1
            self.host_counts[host] += 1
            if orgnr:
                self.per_org_requests[orgnr] += 1

    def record_response(self, *, status: int | None, latency_ms: float, bytes_received: int = 0, error: str | None = None, orgnr: str | None = None) -> None:
        with self._lock:
            self.latency_ms.append(latency_ms)
            if orgnr:
                self.per_org_latency_ms[orgnr] += max(0, int(latency_ms))
            self.bytes_received += max(0, bytes_received)
            self.status_counts[str(status) if status is not None else "network_error"] += 1
            if error:
                self.failures += 1
            if status in (401, 403, 407, 429):
                self.blocked += 1

    def record_retry(self, orgnr: str | None = None) -> None:
        with self._lock:
            self.retries += 1
            if orgnr: self.per_org_retries[orgnr] += 1

    def record_cache_hit(self, orgnr: str | None = None) -> None:
        with self._lock:
            self.cache_hits += 1
            if orgnr: self.per_org_cache_hits[orgnr] += 1

    def record_cost(self, amount: float) -> None:
        with self._lock:
            new_amount = self.cost_usd + amount
            if new_amount > self.max_cost_usd + 1e-9:
                raise BudgetExceeded("declared external API cost budget exhausted")
            self.cost_usd = new_amount

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started_monotonic) * 1000)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            lats = sorted(self.latency_ms)
            p95 = lats[min(len(lats) - 1, int(len(lats) * 0.95))] if lats else 0.0
            return {
                "requests": self.requests,
                "runtime_ms": self.elapsed_ms,
                "per_domain_caps": {"registered_brreg_domain": self.max_requests_per_registry_domain,
                                    "other_domain": self.max_requests_per_domain},
                "third_party_cost_usd": round(self.cost_usd, 6),
                "cache_hits": self.cache_hits,
                "retries": self.retries,
                "failures": self.failures,
                "blocked_responses": self.blocked,
                "status_counts": dict(self.status_counts),
                "top_hosts": dict(self.host_counts.most_common(20)),
                "bytes_received": self.bytes_received,
                "p95_latency_ms": round(p95, 2),
            }
