from __future__ import annotations

import datetime as dt
import json
from typing import Any

ALLOWED_STATES = {"available", "not_available", "blocked", "not_applicable", "ambiguous", "failed"}

def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

class EnvelopeBuilder:
    def __init__(self, organisation_number: str, run_id: str, started_at: str):
        self.orgnr = organisation_number
        self.run_id = run_id
        self.started_at = started_at
        self.claims: list[dict[str, Any]] = []
        self.evidence: list[dict[str, Any]] = []
        self.sources: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []
        self._evidence_idx = 0
        self._field_keys: set[tuple[str, str]] = set()

    def add_evidence(self, *, source_url: str, source_title: str, source_class: str, retrieved_at: str,
                     content_sha256: str, claim_span: str, identity_match_status: str = "verified",
                     confidence: float = 1.0, published_at: str | None = None, effective_date: str | None = None,
                     snapshot_id: str | None = None, http_status: int | None = None,
                     content_type: str | None = None, validation_notes: str | None = None) -> str:
        # A fact-specific evidence record can cite an immutable byte snapshot.
        self._evidence_idx += 1
        eid = f"ev-{self.orgnr}-{self._evidence_idx}"
        evidence = {
            "id": eid,
            "organisation_number": self.orgnr,
            "source_url": source_url,
            "source_title": source_title,
            "source_class": source_class,
            "retrieved_at": retrieved_at,
            "published_at": published_at,
            "effective_date": effective_date,
            "content_sha256": content_sha256,
            "snapshot_id": snapshot_id,
            "http_status": http_status,
            "content_type": content_type,
            "claim_span": str(claim_span)[:1200],
            "identity_match_status": identity_match_status,
            "confidence": round(max(0.0, min(1.0, confidence)), 3),
            "validation_notes": validation_notes,
        }
        self.evidence.append(evidence)
        return eid

    def add_source_snapshot(self, snapshot: dict[str, Any]) -> None:
        candidate = {
            "id": snapshot.get("id"), "source_url": snapshot.get("source_url"),
            "source_class": snapshot.get("source_class"), "retrieved_at": snapshot.get("retrieved_at"),
            "http_status": snapshot.get("http_status"), "content_type": snapshot.get("content_type"),
            "content_sha256": snapshot.get("content_sha256"),
            "identity_match_status": snapshot.get("identity_match_status"),
        }
        if candidate not in self.sources: self.sources.append(candidate)

    def add_claim(self, field: str, value: Any = None, *, availability: str = "available", confidence: float = 0.9,
                  evidence_ids: list[str] | None = None, reporting_period: str | None = None,
                  effective_date: str | None = None, notes: str | None = None) -> None:
        if availability not in ALLOWED_STATES:
            raise ValueError(f"invalid availability state: {availability}")
        # Deterministic de-duplication. Different values for the same field remain separately traceable.
        signature = (field, json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        if signature in self._field_keys: return
        self._field_keys.add(signature)
        self.claims.append({
            "field": field, "value": value, "availability": availability,
            "confidence": round(max(0.0, min(1.0, confidence)), 3),
            "evidence_ids": list(dict.fromkeys(evidence_ids or [])),
            "reporting_period": reporting_period,
            "effective_date": effective_date,
            "notes": notes,
        })

    def add_error(self, *, code: str, message: str, source_url: str | None = None,
                  retryable: bool = False, occurred_at: str | None = None) -> None:
        self.errors.append({"code": code, "message": message[:500], "source_url": source_url,
                            "retryable": bool(retryable), "occurred_at": occurred_at or now_iso()})

    def finalize(self, *, terminal_status: str, operations: dict[str, Any], completed_at: str | None = None,
                 identity_anchor: dict[str, Any] | None = None, summary: str = "", changes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return {
            "organisation_number": self.orgnr,
            "summary": summary,
            "identity_anchor": identity_anchor,
            "run": {"run_id": self.run_id, "started_at": self.started_at,
                    "completed_at": completed_at or now_iso(), "terminal_status": terminal_status},
            "claims": self.claims,
            "evidence": self.evidence,
            "source_snapshots": self.sources,
            "changes": changes or [],
            "errors": self.errors,
            "operations": operations,
        }
