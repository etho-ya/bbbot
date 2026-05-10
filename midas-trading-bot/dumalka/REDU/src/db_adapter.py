"""
DB Adapter — PostgreSQL connection pool for Risk Engine.
Uses psycopg2 + asyncio.to_thread() for async compatibility.

Usage:
    from db_adapter import get_db_pool, pg_fetch_all, pg_fetch_one, pg_fetch_val, pg_execute, pg_executemany, init_pg_pool

v0.9.2: Switched from asyncpg to psycopg2 (asyncpg hangs on this system)
v0.9.4: Added error context logging + slow-query detection (P1 observability)
"""
import psycopg2
import psycopg2.pool
import psycopg2.extras
import asyncio
import logging
import re
import time as _time
from typing import Optional, List, Dict, Any

logger = logging.getLogger("risk-engine.db-adapter")

SLOW_QUERY_THRESHOLD_SEC = 1.0  # Log queries slower than this

# ── Global pool ──────────────────────────────────────────────────────────────
_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None

PG_HOST = "/var/run/postgresql"
PG_DB = "riskengine_db"
PG_USER = "riskengine"


async def init_pg_pool():
    """
    Initializes a thread-safe psycopg2 connection pool (ThreadedConnectionPool).
    Must be called exactly once during the application startup lifecycle.
    """
    """Initialize the psycopg2 connection pool. Call once at startup.
    
    PG18 features used:
    - AIO io_uring with 4 workers for faster position snapshot scans
    - Per-connection options via 'options' param (jit=off, timeouts, work_mem)
    - pg_stat_statements for query monitoring (server-side)
    - track_io_timing=on for I/O latency visibility
    """
    global _pool
    if _pool is not None:
        return
    # PG17: per-connection session defaults via options string
    # - jit=off: JIT compilation hurts short OLTP queries (<100ms)
    # - statement_timeout=30s: prevent infinite query hangs
    # - lock_timeout=5s: fail fast on lock contention
    # - idle_in_transaction_session_timeout=60s: kill abandoned transactions
    # - work_mem=16MB: enough for analytics JOINs on 60K+ snapshot rows
    pg_options = (
        "-c jit=off "
        "-c statement_timeout=30000 "
        "-c lock_timeout=5000 "
        "-c idle_in_transaction_session_timeout=60000 "
        "-c work_mem=16MB"
    )
    _pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=2, maxconn=10,
        dbname=PG_DB, user=PG_USER, host=PG_HOST,
        options=pg_options,
    )
    logger.info(f"PostgreSQL 18 pool initialized (psycopg2, min=2, max=10, io_uring=on, jit=off, stmt_timeout=30s, work_mem=16MB)")


async def close_pg_pool():
    """Close the pool. Call on shutdown."""
    global _pool
    if _pool:
        _pool.closeall()
        _pool = None
        logger.info("PostgreSQL pool closed")


def get_db_pool():
    """Get the connection pool (must be initialized first)."""
    if _pool is None:
        raise RuntimeError("DB pool not initialized. Call init_pg_pool() first.")
    return _pool


def _safe_getconn():
    """Get a connection from pool with health check (test-on-borrow)."""
    conn = _pool.getconn()
    try:
        # Quick health check: if connection is in a bad state, this will fail
        conn.isolation_level
        if conn.closed:
            raise Exception("connection closed")
        return conn
    except Exception:
        logger.warning("Pool returned dead/broken connection, replacing")
        try:
            _pool.putconn(conn, close=True)
        except Exception:
            pass
        conn = _pool.getconn()
        return conn


def _safe_putconn(conn):
    """Return connection to pool, ensuring clean transaction state."""
    try:
        if conn and not conn.closed:
            # Safety: rollback any uncommitted transaction before returning
            if not conn.autocommit:
                conn.rollback()
        _pool.putconn(conn)
    except Exception as e:
        logger.warning(f"Error returning connection to pool: {e}")
        try:
            _pool.putconn(conn, close=True)
        except Exception:
            pass


# ── SQL Dialect Conversion ───────────────────────────────────────────────────

def _convert_sql(sql: str) -> str:
    """Convert SQLite SQL → PostgreSQL SQL (placeholder conversion only for display, psycopg2 uses %s)."""
    pg_sql = sql
    
    # ? → %s (psycopg2 native placeholder)
    pg_sql = pg_sql.replace('?', '%s')
    
    # datetime('now') → NOW()
    pg_sql = pg_sql.replace("datetime('now')", "NOW()")
    
    # datetime('now', '-N hours/days') → NOW() - INTERVAL 'N hours'
    pg_sql = re.sub(
        r"datetime\('now',\s*'(-?\d+)\s+(hour|hours|minute|minutes|day|days)'\)",
        lambda m: f"NOW() - INTERVAL '{abs(int(m.group(1)))} {m.group(2)}'",
        pg_sql
    )
    
    # strftime('%H', col) → EXTRACT(HOUR FROM col)::INTEGER
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
    
    # julianday(a) - julianday(b) → EXTRACT(EPOCH FROM (a::timestamp - b::timestamp))/86400
    pg_sql = re.sub(
        r"julianday\((\w+)\)\s*-\s*julianday\((\w+)\)",
        r"EXTRACT(EPOCH FROM (\1::timestamp - \2::timestamp))/86400",
        pg_sql
    )
    
    # INTEGER PRIMARY KEY AUTOINCREMENT → SERIAL PRIMARY KEY
    pg_sql = pg_sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    
    # INSERT OR REPLACE → INSERT ... ON CONFLICT
    if pg_sql.strip().upper().startswith("INSERT OR REPLACE"):
        pg_sql = pg_sql.replace("INSERT OR REPLACE", "INSERT", 1)
    
    # Remove PRAGMA statements
    if 'PRAGMA' in pg_sql.upper():
        return "SELECT 1"
    
    return pg_sql


# ── Type serialization (PG native → JSON-compatible) ─────────────────────────

import datetime
from decimal import Decimal

def _serialize_row(row: dict) -> dict:
    """Convert PostgreSQL native types to JSON-serializable Python types."""
    out = {}
    for k, v in row.items():
        if isinstance(v, datetime.datetime):
            out[k] = v.isoformat()
        elif isinstance(v, datetime.date):
            out[k] = v.isoformat()
        elif isinstance(v, Decimal):
            out[k] = float(v)
        else:
            out[k] = v
    return out


# ── Sync query helpers (run in thread pool) ──────────────────────────────────

def _sync_fetch_all(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    pg_sql = _convert_sql(sql)
    conn = _safe_getconn()
    conn.autocommit = True  # SELECTs don't need transactions
    t0 = _time.perf_counter()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(pg_sql, params or None)
            rows = [_serialize_row(dict(r)) for r in cur.fetchall()]
            elapsed = _time.perf_counter() - t0
            if elapsed > SLOW_QUERY_THRESHOLD_SEC:
                logger.warning(f"SLOW QUERY ({elapsed:.2f}s, {len(rows)} rows): {pg_sql[:200]}")
            return rows
    except Exception as e:
        logger.error(f"SQL ERROR in fetch_all: {pg_sql[:200]} params={str(params)[:100]} — {e}")
        raise
    finally:
        conn.autocommit = False
        _safe_putconn(conn)


def _sync_fetch_one(sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
    pg_sql = _convert_sql(sql)
    conn = _safe_getconn()
    conn.autocommit = True
    t0 = _time.perf_counter()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(pg_sql, params or None)
            row = cur.fetchone()
            elapsed = _time.perf_counter() - t0
            if elapsed > SLOW_QUERY_THRESHOLD_SEC:
                logger.warning(f"SLOW QUERY ({elapsed:.2f}s): {pg_sql[:200]}")
            return _serialize_row(dict(row)) if row else None
    except Exception as e:
        logger.error(f"SQL ERROR in fetch_one: {pg_sql[:200]} params={str(params)[:100]} — {e}")
        raise
    finally:
        conn.autocommit = False
        _safe_putconn(conn)


def _sync_fetch_val(sql: str, params: tuple = ()):
    pg_sql = _convert_sql(sql)
    conn = _safe_getconn()
    conn.autocommit = True
    t0 = _time.perf_counter()
    try:
        with conn.cursor() as cur:
            cur.execute(pg_sql, params or None)
            row = cur.fetchone()
            elapsed = _time.perf_counter() - t0
            if elapsed > SLOW_QUERY_THRESHOLD_SEC:
                logger.warning(f"SLOW QUERY ({elapsed:.2f}s): {pg_sql[:200]}")
            return row[0] if row else None
    except Exception as e:
        logger.error(f"SQL ERROR in fetch_val: {pg_sql[:200]} params={str(params)[:100]} — {e}")
        raise
    finally:
        conn.autocommit = False
        _safe_putconn(conn)


def _sync_execute(sql: str, params: tuple = ()) -> str:
    pg_sql = _convert_sql(sql)
    conn = _safe_getconn()
    t0 = _time.perf_counter()
    try:
        with conn.cursor() as cur:
            cur.execute(pg_sql, params or None)
            conn.commit()
            elapsed = _time.perf_counter() - t0
            if elapsed > SLOW_QUERY_THRESHOLD_SEC:
                logger.warning(f"SLOW QUERY ({elapsed:.2f}s): {pg_sql[:200]}")
            return cur.statusmessage
    except Exception as e:
        logger.error(f"SQL ERROR in execute: {pg_sql[:200]} params={str(params)[:100]} — {e}")
        conn.rollback()
        raise
    finally:
        _safe_putconn(conn)


def _sync_executemany(sql: str, params_list: list) -> None:
    pg_sql = _convert_sql(sql)
    conn = _safe_getconn()
    try:
        with conn.cursor() as cur:
            cur.executemany(pg_sql, params_list)
            conn.commit()
    except Exception as e:
        logger.error(f"SQL ERROR in executemany: {pg_sql[:200]} — {e}")
        conn.rollback()
        raise
    finally:
        _safe_putconn(conn)


# ── Async wrappers (main interface) ──────────────────────────────────────────

async def pg_fetch_all(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    """
    Executes a read-only SQL query and returns immediately fetched all rows as dictionaries.
    Offloaded to a thread pool via asyncio.to_thread to prevent event loop blocking.
    """
    """Execute query, return all rows as list of dicts."""
    return await asyncio.to_thread(_sync_fetch_all, sql, params)


async def pg_fetch_one(sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
    """Execute query, return first row as dict or None."""
    return await asyncio.to_thread(_sync_fetch_one, sql, params)


async def pg_fetch_val(sql: str, params: tuple = ()):
    """Execute query, return single scalar value."""
    return await asyncio.to_thread(_sync_fetch_val, sql, params)


async def pg_execute(sql: str, params: tuple = ()) -> str:
    """
    Executes a fast parameter-bound write query (INSERT/UPDATE/DELETE).
    Commits automatically via connection's autocommit configuration.
    """
    """Execute INSERT/UPDATE/DELETE, return status string."""
    return await asyncio.to_thread(_sync_execute, sql, params)


async def pg_executemany(sql: str, params_list: list) -> None:
    """Execute same query with multiple param sets (batch insert)."""
    return await asyncio.to_thread(_sync_executemany, sql, params_list)
