-- ============================================================================
-- Risk Engine v0.9.x
-- SQLite Production-Grade Schema Prototype
-- ============================================================================
-- This prototype fixes severe technical debt in the v0.8.4 database:
-- 1. Adds missing Foreign Keys (Data Integrity).
-- 2. Adds missing Indices strictly targeted for application queries (Performance).
-- 3. Enables Write-Ahead Logging (WAL) implicitly via application connection.
-- ============================================================================

-- Enforce Foreign Keys support (must be executed per connection, but declared here for documentation)
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- ----------------------------------------------------------------------------
-- 1. Signals (Incoming Trade Suggestions)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS signals_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_hash TEXT NOT NULL UNIQUE,      -- UNIQUE constraint prevents duplicate signals
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT CHECK(side IN ('long', 'short')),
    price_at_signal REAL NOT NULL,
    
    -- Midas Metadata
    midas_risk_reward REAL,
    midas_probability REAL,
    midas_win_rate REAL,
    trend_alignment REAL,                  -- v0.8.4 legacy: score 0-1
    
    -- Risk Engine Assessment
    re_signal_score REAL,
    re_recommendation TEXT CHECK(re_recommendation IN ('approve', 'reduce', 'reject')),
    var_99_pct REAL,
    cvar_pct REAL,
    
    payload_raw JSON NOT NULL,
    score_components TEXT,
    setup_master_text TEXT
);

-- Optimization: Fast lookup when Telegram Trade Events arrive
CREATE INDEX IF NOT EXISTS idx_signals_hash ON signals_v2(signal_hash);
CREATE INDEX IF NOT EXISTS idx_signals_created_at ON signals_v2(created_at);

-- ----------------------------------------------------------------------------
-- 2. Open Positions (Active Trades managed by the bot)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS open_positions_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_hash TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT CHECK(side IN ('long', 'short')),
    entry_price REAL NOT NULL,
    current_price REAL NOT NULL,
    size REAL NOT NULL,
    
    -- Dynamic State Tracking (v0.8.4 fully replicated)
    current_sl REAL,
    current_tp1 REAL,
    current_tp2 REAL,
    current_tp3 REAL,
    
    current_pnl_pct REAL DEFAULT 0,
    max_pnl_pct REAL DEFAULT 0,
    max_price_favorable REAL,
    drawdown_from_peak_pct REAL DEFAULT 0,
    tp_progress_pct REAL DEFAULT 0,
    
    zone INTEGER DEFAULT 0,
    closed_fraction REAL DEFAULT 0,
    
    status TEXT DEFAULT 'open' CHECK(status IN ('open', 'closed')),
    closed_at TEXT,
    close_reason TEXT,
    close_reason_detailed TEXT,
    realized_pnl_pct REAL,
    realized_pnl_usdt REAL,
    
    -- DBA Constraint 1: Financial sanity checks
    CHECK(size > 0 AND entry_price > 0),
    FOREIGN KEY(signal_hash) REFERENCES signals_v2(signal_hash) ON DELETE CASCADE
);

-- Optimization: Fast query for the tracking loop `WHERE status = 'open'`
CREATE INDEX IF NOT EXISTS idx_open_pos_status ON open_positions_v2(status);
-- Optimization: Fast lookup when correlating signals to positions
CREATE INDEX IF NOT EXISTS idx_open_pos_hash ON open_positions_v2(signal_hash);

-- ----------------------------------------------------------------------------
-- 3. Trade Outcomes (Ledger / Event Log)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trade_outcomes_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_hash TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN ('open','tp1_hit','tp2_hit','tp3_hit','sl_hit','full_close','timeout','partial_close')),
    event_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    pnl_pct REAL,
    
    -- DBA Constraint 2: Financial Ledgers must NEVER cascade delete.
    -- If a signal is purged, the trade outcome history MUST remain or abort the purge.
    FOREIGN KEY(signal_hash) REFERENCES signals_v2(signal_hash) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_outcomes_hash_event ON trade_outcomes_v2(signal_hash, event_type);

-- ----------------------------------------------------------------------------
-- 4. ML Telemetry (Position Snapshots)
-- ----------------------------------------------------------------------------
-- WARNING: This table grows by ~1,500 rows/day. 
-- In v0.9.x, we must partition this by month or strictly enforce indices.
CREATE TABLE IF NOT EXISTS position_snapshots_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pos_id INTEGER NOT NULL,
    
    -- DBA Optimization 1: TEXT ISO-8601 takes 24 bytes/row on disk.
    -- INTEGER Unix Epoch takes 4-8 bytes. For 100M ML rows, this saves ~1.5GB of SSD and memory cache!
    snapshot_at_ts INTEGER NOT NULL,
    
    -- Telemetry & Monte Carlo Features
    pnl_pct REAL,
    max_pnl_pct REAL,
    drawdown_pct REAL,
    tp_progress_pct REAL,
    hours_open REAL,
    zone INTEGER,
    volatility REAL,
    volume_ratio REAL,
    mc_p_tp REAL,
    mc_p_sl REAL,
    mc_var REAL,
    signal_score REAL,
    signal_hash TEXT,
    
    -- ML Extended Features (v0.8.4 additions)
    funding_rate REAL,
    oi_change_pct REAL,
    regime TEXT,
    spread_pct REAL,
    trend_sum REAL,
    
    -- Target Variables for ML
    action_taken TEXT,
    optimal_action TEXT,  -- Retrospectively labeled by label_optimal_actions.py
    future_pnl_1h REAL,
    future_pnl_4h REAL,
    
    FOREIGN KEY(pos_id) REFERENCES open_positions_v2(id) ON DELETE CASCADE
);

-- DBA Optimization 2: Composite Index for ML Timeseries Extraction
-- Typical Query: `SELECT * FROM position_snapshots WHERE pos_id = ? ORDER BY snapshot_at_ts DESC`
-- A single index on pos_id forces an in-memory sort. The composite index eliminates sorting entirely.
CREATE INDEX IF NOT EXISTS idx_snapshots_pos_time ON position_snapshots_v2(pos_id, snapshot_at_ts DESC);

-- Optimization: For temporal aggregation and cleanup scripts
CREATE INDEX IF NOT EXISTS idx_snapshots_time ON position_snapshots_v2(snapshot_at_ts);
-- Optimization: For filtering only labeled data during XGBoost training
CREATE INDEX IF NOT EXISTS idx_snapshots_optimal ON position_snapshots_v2(optimal_action) WHERE optimal_action IS NOT NULL;

-- ============================================================================
-- 5. Auxiliary Tables (Analytics, Calibration, Alerts) 
-- ============================================================================

CREATE TABLE IF NOT EXISTS dumalka_audit_log_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    trace_id TEXT,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    mc_diagnostics TEXT,
    market_state TEXT
);

CREATE TABLE IF NOT EXISTS watchlist_alerts_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    price_change_1h REAL,
    price_change_4h REAL,
    volatility REAL,
    funding_rate REAL,
    re_score REAL,
    re_recommendation TEXT,
    multi_tf_trends TEXT,
    alert_sent INTEGER DEFAULT 0,
    -- Post-mortem ML labeled fields
    price_after_1h REAL,
    price_after_4h REAL,
    would_have_pnl_pct REAL
);
CREATE INDEX IF NOT EXISTS idx_wa_symbol ON watchlist_alerts_v2(symbol);
CREATE INDEX IF NOT EXISTS idx_wa_created ON watchlist_alerts_v2(created_at);

CREATE TABLE IF NOT EXISTS analytics_cache_v2 (
    metric_key TEXT PRIMARY KEY,
    data_json TEXT NOT NULL,
    computed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS what_if_outcomes_v2 (
    pos_id INTEGER PRIMARY KEY,
    symbol TEXT, 
    side TEXT,
    close_reason TEXT,
    exit_pnl REAL,
    what_if TEXT,
    missed_pnl REAL,
    hours_to_outcome INTEGER,
    analyzed_at TEXT
);

CREATE TABLE IF NOT EXISTS zone_calibration_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    calibrated_at TEXT NOT NULL,
    zone_1_dd_thresh REAL NOT NULL,
    zone_2_dd_thresh REAL NOT NULL,
    zone_3_dd_thresh REAL NOT NULL,
    zone_4_dd_thresh REAL NOT NULL,
    capture_ratio_avg REAL,
    capture_ratio_improvement REAL,
    n_positions_used INTEGER,
    is_active INTEGER DEFAULT 1,
    notes TEXT
);

-- ============================================================================
-- End of Schema
-- ============================================================================
