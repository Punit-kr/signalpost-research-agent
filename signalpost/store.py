from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import threading
from functools import wraps
from pathlib import Path
from typing import Any


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _db_locked(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class StateStore:
    """SQLite run history plus content-addressed source snapshots. Safe for idempotent replay."""
    def __init__(self, db_path: str | Path, snapshot_dir: str | Path):
        self.db_path = Path(db_path)
        self.snapshot_dir = Path(snapshot_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._snapshot_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self._init_db()

    def _init_db(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS source_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            organisation_number TEXT NOT NULL,
            source_url TEXT NOT NULL,
            source_class TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            http_status INTEGER,
            content_type TEXT,
            content_sha256 TEXT NOT NULL,
            snapshot_path TEXT NOT NULL,
            identity_match_status TEXT NOT NULL,
            excerpt TEXT,
            run_id TEXT NOT NULL,
            UNIQUE(organisation_number, source_url, content_sha256)
        );
        CREATE INDEX IF NOT EXISTS idx_snapshots_org_url ON source_snapshots(organisation_number, source_url);
        CREATE TABLE IF NOT EXISTS profile_history (
            history_id INTEGER PRIMARY KEY AUTOINCREMENT,
            organisation_number TEXT NOT NULL,
            run_id TEXT NOT NULL,
            saved_at TEXT NOT NULL,
            profile_sha256 TEXT NOT NULL,
            envelope_json TEXT NOT NULL,
            UNIQUE(organisation_number, profile_sha256)
        );
        CREATE TABLE IF NOT EXISTS current_profiles (
            organisation_number TEXT PRIMARY KEY,
            profile_sha256 TEXT NOT NULL,
            envelope_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS claim_history (
            organisation_number TEXT NOT NULL,
            field TEXT NOT NULL,
            value_sha256 TEXT NOT NULL,
            value_json TEXT NOT NULL,
            availability TEXT NOT NULL,
            evidence_ids_json TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            run_id TEXT NOT NULL,
            PRIMARY KEY(organisation_number, field, value_sha256)
        );
        CREATE TABLE IF NOT EXISTS http_cache (
            url TEXT PRIMARY KEY,
            fetched_epoch REAL NOT NULL,
            status INTEGER,
            headers_json TEXT NOT NULL,
            body BLOB NOT NULL,
            content_sha256 TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS run_log (
            run_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            report_json TEXT
        );
        """)
        self.conn.commit()

    @_db_locked
    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    @_db_locked
    def begin_run(self, run_id: str, started_at: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO run_log(run_id, started_at) VALUES (?, ?)", (run_id, started_at))
        self.conn.commit()

    @_db_locked
    def end_run(self, run_id: str, completed_at: str, report: dict[str, Any]) -> None:
        self.conn.execute("UPDATE run_log SET completed_at=?, report_json=? WHERE run_id=?", (completed_at, stable_json(report), run_id))
        self.conn.commit()

    @_db_locked
    def cache_get(self, url: str, ttl_seconds: float) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM http_cache WHERE url=?", (url,)).fetchone()
        if not row or time.time() - float(row["fetched_epoch"]) > ttl_seconds:
            return None
        return {"status": row["status"], "headers": json.loads(row["headers_json"]), "body": bytes(row["body"]), "content_sha256": row["content_sha256"], "fetched_epoch": row["fetched_epoch"]}

    @_db_locked
    def cache_put(self, url: str, status: int | None, headers: dict[str, str], body: bytes, content_sha256: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO http_cache(url,fetched_epoch,status,headers_json,body,content_sha256) VALUES (?,?,?,?,?,?)", (url, time.time(), status, stable_json(headers), sqlite3.Binary(body), content_sha256))
        self.conn.commit()

    @_db_locked
    def save_snapshot(self, *, organisation_number: str, source_url: str, source_class: str, fetched_at: str,
                      http_status: int | None, content_type: str | None, body: bytes, identity_match_status: str,
                      excerpt: str, run_id: str) -> dict[str, Any]:
        digest = sha256_bytes(body)
        cache_key = (organisation_number, source_url, digest)
        cached = self._snapshot_cache.get(cache_key)
        if cached is not None:
            return {**cached, "excerpt": excerpt[:4000]}
        path = self.snapshot_dir / f"{digest}.bin"
        if not path.exists():
            temp = path.with_suffix(".tmp")
            temp.write_bytes(body)
            os.replace(temp, path)
        url_hash = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:8]
        snapshot_id = f"snap-{organisation_number}-{url_hash}-{digest[:20]}"
        self.conn.execute("""INSERT OR IGNORE INTO source_snapshots
            (snapshot_id, organisation_number, source_url, source_class, fetched_at, http_status, content_type, content_sha256, snapshot_path, identity_match_status, excerpt, run_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (snapshot_id, organisation_number, source_url, source_class, fetched_at, http_status, content_type, digest, str(path), identity_match_status, excerpt[:4000], run_id))
        self.conn.commit()
        result = {"id": snapshot_id, "organisation_number": organisation_number, "source_url": source_url,
                "source_class": source_class, "retrieved_at": fetched_at, "http_status": http_status,
                "content_type": content_type, "content_sha256": digest, "snapshot_path": str(path),
                "identity_match_status": identity_match_status, "excerpt": excerpt[:4000]}
        self._snapshot_cache[cache_key] = result
        return result

    @_db_locked
    def get_current_profile(self, orgnr: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT envelope_json FROM current_profiles WHERE organisation_number=?", (orgnr,)).fetchone()
        return json.loads(row[0]) if row else None

    @_db_locked
    def save_profile(self, orgnr: str, run_id: str, envelope: dict[str, Any], saved_at: str) -> dict[str, Any]:
        # Fingerprint material claims, not timestamps, evidence IDs, or request metrics. A cache
        # refresh of byte-identical facts must not create a false new profile version.
        material_claims = []
        for claim in envelope.get("claims", []):
            material_claims.append({
                "field": claim.get("field"), "value": claim.get("value"),
                "availability": claim.get("availability"),
                "reporting_period": claim.get("reporting_period"),
                "effective_date": claim.get("effective_date"),
            })
        profile_payload = {"organisation_number": orgnr,
            "identity_anchor": {k: (envelope.get("identity_anchor") or {}).get(k)
                                for k in ("organisation_number", "official_name", "match_status")},
            "claims": sorted(material_claims, key=lambda c: (str(c.get("field")), stable_json(c.get("value"))))}
        profile_hash = sha256_bytes(stable_json(profile_payload).encode("utf-8"))
        self.conn.execute("INSERT OR IGNORE INTO profile_history(organisation_number,run_id,saved_at,profile_sha256,envelope_json) VALUES (?,?,?,?,?)",
                          (orgnr, run_id, saved_at, profile_hash, stable_json(envelope)))
        self.conn.execute("INSERT OR REPLACE INTO current_profiles(organisation_number,profile_sha256,envelope_json,updated_at) VALUES (?,?,?,?)",
                          (orgnr, profile_hash, stable_json(envelope), saved_at))
        for claim in envelope.get("claims", []):
            value_json = stable_json(claim.get("value"))
            value_hash = sha256_bytes(value_json.encode("utf-8"))
            self.conn.execute("""INSERT INTO claim_history(organisation_number,field,value_sha256,value_json,availability,evidence_ids_json,first_seen_at,last_seen_at,run_id)
                VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(organisation_number,field,value_sha256) DO UPDATE SET
                last_seen_at=excluded.last_seen_at, evidence_ids_json=excluded.evidence_ids_json, run_id=excluded.run_id""",
                (orgnr, claim.get("field", ""), value_hash, value_json, claim.get("availability", "available"), stable_json(claim.get("evidence_ids", [])), saved_at, saved_at, run_id))
        self.conn.commit()
        return {"profile_sha256": profile_hash, "stored": True}

    @_db_locked
    def count_history(self, orgnr: str | None = None) -> int:
        if orgnr:
            row = self.conn.execute("SELECT COUNT(*) FROM profile_history WHERE organisation_number=?", (orgnr,)).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) FROM profile_history").fetchone()
        return int(row[0])
