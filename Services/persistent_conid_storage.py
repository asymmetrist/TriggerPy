# persistent_conid_storage.py

import sqlite3
from datetime import datetime, timedelta
from typing import Optional


class PersistentConidStorage:
    def __init__(self, db_path: str = "conids.db"):
        self.db_path = db_path
        self._init_db()

    def _get_conn(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        with self._get_conn() as conn:
            # Stock conIDs table
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conids (
                    symbol TEXT PRIMARY KEY,
                    conid TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            # Option conIDs table
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS option_conids (
                    symbol TEXT NOT NULL,
                    expiry TEXT NOT NULL,
                    strike REAL NOT NULL,
                    right TEXT NOT NULL,
                    conid TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, expiry, strike, right)
                )
                """
            )
            # Symbol search cache table
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS symbol_search_cache (
                    query TEXT PRIMARY KEY,
                    results TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            # Work symbols list table (persistence)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS work_symbols_list (
                    symbol TEXT PRIMARY KEY,
                    added_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def store_conid(self, symbol: str, conid: str) -> None:
        """
        Insert or update a conid for a given symbol.
        """
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO conids (symbol, conid, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(symbol)
                DO UPDATE SET
                    conid = excluded.conid,
                    updated_at = excluded.updated_at
                """,
                (symbol, conid, now),
            )
            conn.commit()

    def get_conid(self, symbol: str) -> Optional[str]:
        """
        Retrieve the conid for a given symbol.
        """
        with self._get_conn() as conn:
            cur = conn.execute(
                "SELECT conid FROM conids WHERE symbol = ?",
                (symbol,),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def get_last_update(self, symbol: str) -> Optional[datetime]:
        """
        Get last update datetime for a symbol.
        """
        with self._get_conn() as conn:
            cur = conn.execute(
                "SELECT updated_at FROM conids WHERE symbol = ?",
                (symbol,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return datetime.fromisoformat(row[0])

    def is_fresh(self, symbol: str, days: int = 7) -> bool:
        """
        Returns True if the last update is newer than `days` (default: 7).
        """
        last_update = self.get_last_update(symbol)
        if not last_update:
            return False
        return datetime.utcnow() - last_update <= timedelta(days=days)

    # ========== Option ConID Methods ==========
    
    def store_option_conid(self, symbol: str, expiry: str, strike: float, right: str, conid: str) -> None:
        """
        Insert or update an option conID for a given symbol/expiry/strike/right combination.
        """
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO option_conids (symbol, expiry, strike, right, conid, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, expiry, strike, right)
                DO UPDATE SET
                    conid = excluded.conid,
                    updated_at = excluded.updated_at
                """,
                (symbol.upper(), expiry, float(strike), right.upper(), conid, now),
            )
            conn.commit()

    def get_option_conid(self, symbol: str, expiry: str, strike: float, right: str) -> Optional[str]:
        """
        Retrieve the option conID for a given symbol/expiry/strike/right combination.
        """
        with self._get_conn() as conn:
            cur = conn.execute(
                "SELECT conid FROM option_conids WHERE symbol = ? AND expiry = ? AND strike = ? AND right = ?",
                (symbol.upper(), expiry, float(strike), right.upper()),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def get_cached_option_conids(self, symbol: str, expiry: str = None) -> list:
        """
        Get all cached option conIDs for a symbol (optionally filtered by expiry).
        Returns list of dicts with keys: expiry, strike, right, conid, updated_at
        """
        with self._get_conn() as conn:
            if expiry:
                cur = conn.execute(
                    "SELECT expiry, strike, right, conid, updated_at FROM option_conids WHERE symbol = ? AND expiry = ?",
                    (symbol.upper(), expiry),
                )
            else:
                cur = conn.execute(
                    "SELECT expiry, strike, right, conid, updated_at FROM option_conids WHERE symbol = ?",
                    (symbol.upper(),),
                )
            rows = cur.fetchall()
            return [
                {
                    "expiry": row[0],
                    "strike": row[1],
                    "right": row[2],
                    "conid": row[3],
                    "updated_at": row[4],
                }
                for row in rows
            ]

    def clear_option_conids(self, symbol: str, expiry: str = None) -> None:
        """
        Clear cached option conIDs for a symbol (optionally filtered by expiry).
        """
        with self._get_conn() as conn:
            if expiry:
                conn.execute(
                    "DELETE FROM option_conids WHERE symbol = ? AND expiry = ?",
                    (symbol.upper(), expiry),
                )
            else:
                conn.execute(
                    "DELETE FROM option_conids WHERE symbol = ?",
                    (symbol.upper(),),
                )
            conn.commit()

    # ========== Symbol Search Cache Methods ==========
    
    def store_symbol_search(self, query: str, results: list) -> None:
        """
        Cache symbol search results.
        query: Search query (e.g., "AAP")
        results: List of symbol dicts from TWS
        """
        import json
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            results_json = json.dumps(results)
            conn.execute(
                """
                INSERT OR REPLACE INTO symbol_search_cache (query, results, updated_at)
                VALUES (?, ?, ?)
                """,
                (query.upper(), results_json, now)
            )
            conn.commit()

    def get_symbol_search(self, query: str) -> Optional[list]:
        """
        Get cached symbol search results.
        Returns None if not cached or cache is stale (>24 hours).
        """
        import json
        with self._get_conn() as conn:
            cur = conn.execute(
                """
                SELECT results FROM symbol_search_cache
                WHERE query = ? AND datetime(updated_at) > datetime('now', '-24 hours')
                """,
                (query.upper(),)
            )
            row = cur.fetchone()
            if row:
                return json.loads(row[0])
        return None

    # ========== Work Symbols List Methods ==========
    
    def save_work_symbol(self, symbol: str) -> None:
        """
        Save a symbol to the work symbols list.
        """
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO work_symbols_list (symbol, added_at)
                VALUES (?, ?)
                """,
                (symbol.upper(), now)
            )
            conn.commit()
    
    def remove_work_symbol(self, symbol: str) -> None:
        """
        Remove a symbol from the work symbols list.
        """
        with self._get_conn() as conn:
            conn.execute(
                "DELETE FROM work_symbols_list WHERE symbol = ?",
                (symbol.upper(),)
            )
            conn.commit()
    
    def load_work_symbols(self) -> list:
        """
        Load all symbols from the work symbols list.
        Returns list of symbol strings.
        """
        with self._get_conn() as conn:
            cur = conn.execute(
                "SELECT symbol FROM work_symbols_list ORDER BY added_at"
            )
            return [row[0] for row in cur.fetchall()]


# Example usage:
storage = PersistentConidStorage()
# storage.store_conid("AAPL", "265598")
# conid = storage.get_conid("AAPL")
# is_recent = storage.is_fresh("AAPL")
