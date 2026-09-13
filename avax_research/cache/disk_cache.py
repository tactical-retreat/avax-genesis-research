"""
SQLite-based disk cache for transaction data.

Transaction data is immutable, so entries never expire.
"""

import hashlib
import json
import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    """A cached entry with metadata."""
    key: str
    value: Any
    created_at: datetime
    chain: str | None = None
    address: str | None = None


class DiskCache:
    """
    SQLite-based cache for immutable blockchain data.

    Thread-safe with connection pooling per thread.
    Supports namespaced keys and optional chain/address metadata for querying.
    """

    def __init__(self, db_path: str | Path = "data/cache/glacier_cache.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Get thread-local database connection."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
            )
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        """Context manager for database cursor."""
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            yield cursor
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()

    def _init_db(self) -> None:
        """Initialize the database schema."""
        with self._cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    chain TEXT,
                    address TEXT,
                    namespace TEXT
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_chain_address
                ON cache (chain, address)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_namespace
                ON cache (namespace)
            """)

    @staticmethod
    def _make_key(namespace: str, *parts: str) -> str:
        """Create a cache key from namespace and parts."""
        combined = ":".join([namespace] + list(parts))
        return hashlib.sha256(combined.encode()).hexdigest()[:32] + ":" + combined[:100]

    def get(self, namespace: str, *parts: str) -> Any | None:
        """
        Get a value from cache.

        Args:
            namespace: Cache namespace (e.g., "tx", "balance")
            parts: Key parts (e.g., chain, address, tx_hash)

        Returns:
            Cached value or None if not found
        """
        key = self._make_key(namespace, *parts)
        with self._cursor() as cursor:
            cursor.execute("SELECT value FROM cache WHERE key = ?", (key,))
            row = cursor.fetchone()
            if row:
                return json.loads(row["value"])
            return None

    def set(
        self,
        namespace: str,
        *parts: str,
        value: Any,
        chain: str | None = None,
        address: str | None = None,
    ) -> None:
        """
        Set a value in cache.

        Args:
            namespace: Cache namespace
            parts: Key parts
            value: Value to cache (must be JSON serializable)
            chain: Optional chain metadata for querying
            address: Optional address metadata for querying
        """
        key = self._make_key(namespace, *parts)
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT OR REPLACE INTO cache (key, value, created_at, chain, address, namespace)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    json.dumps(value),
                    datetime.now(UTC).isoformat(),
                    chain,
                    address,
                    namespace,
                ),
            )

    def get_by_address(self, address: str, chain: str | None = None) -> list[CacheEntry]:
        """Get all cached entries for an address."""
        with self._cursor() as cursor:
            if chain:
                cursor.execute(
                    "SELECT * FROM cache WHERE address = ? AND chain = ?",
                    (address, chain),
                )
            else:
                cursor.execute("SELECT * FROM cache WHERE address = ?", (address,))

            return [
                CacheEntry(
                    key=row["key"],
                    value=json.loads(row["value"]),
                    created_at=datetime.fromisoformat(row["created_at"]),
                    chain=row["chain"],
                    address=row["address"],
                )
                for row in cursor.fetchall()
            ]

    def get_by_namespace(self, namespace: str, limit: int = 1000) -> list[CacheEntry]:
        """Get all cached entries in a namespace."""
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT * FROM cache WHERE namespace = ? LIMIT ?",
                (namespace, limit),
            )
            return [
                CacheEntry(
                    key=row["key"],
                    value=json.loads(row["value"]),
                    created_at=datetime.fromisoformat(row["created_at"]),
                    chain=row["chain"],
                    address=row["address"],
                )
                for row in cursor.fetchall()
            ]

    def exists(self, namespace: str, *parts: str) -> bool:
        """Check if a key exists in cache."""
        key = self._make_key(namespace, *parts)
        with self._cursor() as cursor:
            cursor.execute("SELECT 1 FROM cache WHERE key = ? LIMIT 1", (key,))
            return cursor.fetchone() is not None

    def delete(self, namespace: str, *parts: str) -> bool:
        """Delete a key from cache. Returns True if key existed."""
        key = self._make_key(namespace, *parts)
        with self._cursor() as cursor:
            cursor.execute("DELETE FROM cache WHERE key = ?", (key,))
            return cursor.rowcount > 0

    def clear_namespace(self, namespace: str) -> int:
        """Clear all entries in a namespace. Returns count deleted."""
        with self._cursor() as cursor:
            cursor.execute("DELETE FROM cache WHERE namespace = ?", (namespace,))
            return cursor.rowcount

    def clear_all(self) -> int:
        """Clear entire cache. Returns count deleted."""
        with self._cursor() as cursor:
            cursor.execute("DELETE FROM cache")
            return cursor.rowcount

    def stats(self) -> dict:
        """Get cache statistics."""
        with self._cursor() as cursor:
            cursor.execute("SELECT COUNT(*) as count FROM cache")
            total = cursor.fetchone()["count"]

            cursor.execute(
                "SELECT namespace, COUNT(*) as count FROM cache GROUP BY namespace"
            )
            by_namespace = {row["namespace"]: row["count"] for row in cursor.fetchall()}

            cursor.execute(
                "SELECT chain, COUNT(*) as count FROM cache WHERE chain IS NOT NULL GROUP BY chain"
            )
            by_chain = {row["chain"]: row["count"] for row in cursor.fetchall()}

            # Get database file size
            file_size = self.db_path.stat().st_size if self.db_path.exists() else 0

        return {
            "total_entries": total,
            "by_namespace": by_namespace,
            "by_chain": by_chain,
            "file_size_mb": file_size / (1024 * 1024),
        }

    def close(self) -> None:
        """Close the database connection for the current thread."""
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
