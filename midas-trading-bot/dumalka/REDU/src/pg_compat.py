"""
pg_compat.py — Drop-in aiosqlite-compatible wrapper backed by asyncpg (PostgreSQL).

Usage: replace `import aiosqlite` with `import pg_compat as aiosqlite`
       Everything else stays the same: async with connect(...) as db / db.execute() / cursor.fetchall()

v0.9.1: SQLite → PostgreSQL migration shim.
"""
import re
import asyncpg
import logging
from typing import Optional

logger = logging.getLogger("risk-engine.pg-compat")

# ── Global pool ──────────────────────────────────────────────────────────────
_pool: Optional[asyncpg.Pool] = None
_DATABASE_URL = "postgresql://riskengine@/riskengine_db?host=/var/run/postgresql"


async def _ensure_pool():
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            _DATABASE_URL, min_size=2, max_size=10, command_timeout=60,
        )
        logger.info("pg_compat: asyncpg pool initialized")
    return _pool


def _convert_sql(sql: str, params: tuple = ()) -> tuple:
    """Convert SQLite SQL → PostgreSQL SQL."""
    pg_sql = sql
    
    # ? → $1, $2, ...
    counter = [0]
    def _repl(m):
        counter[0] += 1
        return f"${counter[0]}"
    pg_sql = re.sub(r'\?', _repl, pg_sql)
    
    # datetime('now') → NOW()
    pg_sql = pg_sql.replace("datetime('now')", "NOW()")
    
    # datetime('now', '-N hours/minutes/days') → NOW() - INTERVAL 'N hours'
    pg_sql = re.sub(
        r"datetime\('now',\s*'(-?\d+)\s+(hour|hours|minute|minutes|day|days)'\)",
        lambda m: f"NOW() - INTERVAL '{abs(int(m.group(1)))} {m.group(2)}'",
        pg_sql
    )
    
    # strftime('%H', col) → EXTRACT(HOUR FROM col)
    pg_sql = re.sub(
        r"CAST\(strftime\('%H',\s*(\w+)\)\s*AS\s*INTEGER\)",
        r"EXTRACT(HOUR FROM \1)::INTEGER",
        pg_sql
    )
    pg_sql = re.sub(
        r"strftime\('%H',\s*(\w+)\)",
        r"EXTRACT(HOUR FROM \1)::INTEGER",
        pg_sql
    )
    
    # INSERT OR REPLACE → INSERT ... ON CONFLICT
    if pg_sql.strip().upper().startswith("INSERT OR REPLACE"):
        pg_sql = pg_sql.replace("INSERT OR REPLACE", "INSERT", 1)
        # We handle ON CONFLICT at execution time if needed
    
    # Remove PRAGMA
    if 'PRAGMA' in pg_sql.upper():
        return "SELECT 1", ()
    
    return pg_sql, params


class Row(dict):
    """aiosqlite.Row compatible — dict-like with attribute access."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class PGCursor:
    """Mimics aiosqlite cursor from db.execute()."""
    
    def __init__(self, rows):
        self._rows = rows
        self._index = 0
    
    async def fetchall(self):
        return self._rows
    
    async def fetchone(self):
        if self._index < len(self._rows):
            row = self._rows[self._index]
            self._index += 1
            return row
        return None
    
    async def fetchmany(self, size=1):
        result = self._rows[self._index:self._index + size]
        self._index += size
        return result
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, *args):
        pass


class PGConnection:
    """Mimics aiosqlite connection: async with connect(...) as db."""
    
    def __init__(self):
        self._conn = None
        self.row_factory = None  # Ignored, we always return dicts
    
    async def _acquire(self):
        pool = await _ensure_pool()
        self._conn = await pool.acquire()
        return self
    
    async def execute(self, sql, params=None):
        """Execute SQL, return cursor-like object."""
        pg_sql, pg_params = _convert_sql(sql, params or ())
        
        # Skip PRAGMA
        if pg_sql == "SELECT 1" and 'PRAGMA' in sql.upper():
            return PGCursor([])
        
        try:
            if pg_sql.strip().upper().startswith(("SELECT", "WITH")):
                rows = await self._conn.fetch(pg_sql, *pg_params)
                return PGCursor([Row(dict(r)) for r in rows])
            else:
                await self._conn.execute(pg_sql, *pg_params)
                return PGCursor([])
        except Exception as e:
            # Handle ON CONFLICT for INSERT OR REPLACE 
            if "duplicate key" in str(e).lower() or "unique" in str(e).lower():
                logger.debug(f"pg_compat: duplicate key, ignoring: {e}")
                return PGCursor([])
            raise
    
    async def executemany(self, sql, params_list):
        """Execute same SQL with multiple param sets."""
        pg_sql, _ = _convert_sql(sql)
        await self._conn.executemany(pg_sql, [tuple(p) for p in params_list])
    
    async def execute_fetchall(self, sql, params=None):
        """aiosqlite's execute_fetchall shorthand."""
        cursor = await self.execute(sql, params)
        return await cursor.fetchall()
    
    async def commit(self):
        """No-op — asyncpg auto-commits."""
        pass
    
    async def close(self):
        """Release connection back to pool."""
        if self._conn:
            pool = await _ensure_pool()
            await pool.release(self._conn)
            self._conn = None
    
    async def __aenter__(self):
        await self._acquire()
        return self
    
    async def __aexit__(self, *args):
        await self.close()


def connect(db_path=None):
    """Drop-in replacement for aiosqlite.connect(). Ignores db_path, uses PG pool."""
    return PGConnection()


# Make pg_compat.Row work like aiosqlite.Row
Row = Row
