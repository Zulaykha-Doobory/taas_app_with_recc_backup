"""
Per-tenant OAuth token storage (encrypted at rest).

Each client company is a "tenant". For each tenant we store their Jira and/or
Azure (Entra) OAuth tokens. Tokens are encrypted with Fernet (symmetric
encryption) using a key from the TAAS_TOKEN_KEY environment variable, so the
SQLite file alone is useless to anyone who copies it.

SQLite is used for simplicity (single file, zero setup). The TokenStore class
is the only thing the rest of the app talks to, so swapping to Postgres later
means changing only this file.

SECURITY NOTES:
- The encryption key (TAAS_TOKEN_KEY) must come from the environment, never
  hardcoded, and must be backed up — losing it means losing all stored tokens.
- Generate one with:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
from __future__ import annotations

import os
import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

try:
    from cryptography.fernet import Fernet
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False


DB_PATH = os.environ.get("TAAS_TOKEN_DB", "./taas_tokens.db")


@dataclass
class StoredToken:
    tenant_id: str
    provider: str            # "jira" | "azure"
    access_token: str
    refresh_token: Optional[str]
    expires_at: float        # epoch seconds; 0 if unknown
    meta: dict               # provider extras (cloud_id, resource, etc.)

    @property
    def is_expired(self) -> bool:
        # treat as expired 60s early to be safe
        return self.expires_at and time.time() > (self.expires_at - 60)


class TokenStore:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._fernet = self._make_fernet()
        self._init_db()

    # ---- encryption -------------------------------------------------------

    def _make_fernet(self):
        if not _HAS_CRYPTO:
            raise RuntimeError(
                "The 'cryptography' package is required for token storage. "
                "Install it with: pip install cryptography")
        key = os.environ.get("TAAS_TOKEN_KEY")
        if not key:
            raise RuntimeError(
                "TAAS_TOKEN_KEY environment variable not set. Generate one with:\n"
                "  python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\"\n"
                "then set it in your environment (and back it up safely).")
        return Fernet(key.encode() if isinstance(key, str) else key)

    def _enc(self, text: str) -> str:
        if not text:
            return ""
        return self._fernet.encrypt(text.encode()).decode()

    def _dec(self, token: str) -> str:
        if not token:
            return ""
        return self._fernet.decrypt(token.encode()).decode()

    # ---- schema -----------------------------------------------------------

    def _init_db(self):
        with sqlite3.connect(self.db_path) as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS tokens (
                    tenant_id     TEXT NOT NULL,
                    provider      TEXT NOT NULL,
                    access_token  TEXT NOT NULL,
                    refresh_token TEXT,
                    expires_at    REAL DEFAULT 0,
                    meta          TEXT DEFAULT '{}',
                    updated_at    REAL,
                    PRIMARY KEY (tenant_id, provider)
                )
            """)

    # ---- API --------------------------------------------------------------

    def save(self, token: StoredToken):
        """Insert or update a tenant's token for a provider."""
        with sqlite3.connect(self.db_path) as con:
            con.execute("""
                INSERT INTO tokens (tenant_id, provider, access_token,
                                    refresh_token, expires_at, meta, updated_at)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(tenant_id, provider) DO UPDATE SET
                    access_token=excluded.access_token,
                    refresh_token=excluded.refresh_token,
                    expires_at=excluded.expires_at,
                    meta=excluded.meta,
                    updated_at=excluded.updated_at
            """, (token.tenant_id, token.provider,
                  self._enc(token.access_token),
                  self._enc(token.refresh_token or ""),
                  token.expires_at,
                  json.dumps(token.meta or {}),
                  time.time()))

    def get(self, tenant_id: str, provider: str) -> Optional[StoredToken]:
        with sqlite3.connect(self.db_path) as con:
            row = con.execute("""
                SELECT access_token, refresh_token, expires_at, meta
                FROM tokens WHERE tenant_id=? AND provider=?
            """, (tenant_id, provider)).fetchone()
        if not row:
            return None
        return StoredToken(
            tenant_id=tenant_id,
            provider=provider,
            access_token=self._dec(row[0]),
            refresh_token=self._dec(row[1]) if row[1] else None,
            expires_at=row[2] or 0,
            meta=json.loads(row[3] or "{}"),
        )

    def delete(self, tenant_id: str, provider: str):
        """Revoke/disconnect a tenant's provider connection."""
        with sqlite3.connect(self.db_path) as con:
            con.execute("DELETE FROM tokens WHERE tenant_id=? AND provider=?",
                        (tenant_id, provider))

    def list_connections(self, tenant_id: str) -> list:
        """Which providers this tenant has connected (no secrets returned)."""
        with sqlite3.connect(self.db_path) as con:
            rows = con.execute(
                "SELECT provider, expires_at FROM tokens WHERE tenant_id=?",
                (tenant_id,)).fetchall()
        return [{"provider": r[0], "expires_at": r[1]} for r in rows]
