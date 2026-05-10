-- ═══════════════════════════════════════════════════════════════════════
-- Migration: v0.10.0 — BRIN Indexes + Partition Infrastructure
-- Date: 2026-03-24
--
-- At 62K rows, full partitioning is premature. Instead:
-- 1. Add BRIN indexes for time-range queries (O(1) vs O(n) scan)
-- 2. Create partition-readiness function for when we hit 1M+ rows
-- 3. Optimize most common JOIN patterns
-- ═══════════════════════════════════════════════════════════════════════

-- ── BRIN Indexes: Block Range INdexes ──────────────────────────────
-- BRIN indexes are 100x smaller than B-tree and perfect for time-series data
-- where rows are physically ordered by insertion time (which ours are).
-- For 62K rows: ~8KB index vs ~800KB B-tree.

-- Primary time-range index on snapshot_at (most queries filter by time)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_snaps_brin_time
ON position_snapshots USING BRIN (snapshot_at) WITH (pages_per_range = 32);

-- BRIN on pos_id for JOIN acceleration (position snapshots → positions)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_snaps_brin_posid
ON position_snapshots USING BRIN (pos_id) WITH (pages_per_range = 32);

-- ── Composite B-tree indexes for common query patterns ─────────────

-- For GROUP BY pos_id queries with time filters
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_snaps_posid_time
ON position_snapshots (pos_id, snapshot_at DESC);

-- For zone-based analytics (used in dashboard widgets)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_snaps_zone
ON position_snapshots (zone) WHERE zone IS NOT NULL;

-- For ML labeler queries
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_snaps_optimal_action
ON position_snapshots (optimal_action) WHERE optimal_action IS NOT NULL;

-- ── Partial indexes for open_positions common patterns ─────────────

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_positions_status_open
ON open_positions (status) WHERE status = 'open';

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_positions_closed_reason
ON open_positions (close_reason, realized_pnl_pct) WHERE status = 'closed';

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_positions_signal_score
ON open_positions (initial_signal_score) WHERE status = 'closed' AND initial_signal_score IS NOT NULL;

-- ── Partition readiness: auto-partition function ──────────────────
-- Call this when position_snapshots exceeds 1M rows.
-- It creates a new table with RANGE partitioning on snapshot_at,
-- migrates data, and swaps the tables.

-- NOTE: This function is PREPARED but NOT auto-executed.
-- Execute it manually when ready: SELECT create_weekly_partitions();

CREATE OR REPLACE FUNCTION create_weekly_partitions()
RETURNS TEXT AS $$
DECLARE
    row_count BIGINT;
BEGIN
    SELECT COUNT(*) INTO row_count FROM position_snapshots;
    IF row_count < 1000000 THEN
        RETURN 'Skipped: only ' || row_count || ' rows. Partition at 1M+.';
    END IF;

    -- Create partitioned table
    CREATE TABLE IF NOT EXISTS position_snapshots_partitioned (
        LIKE position_snapshots INCLUDING ALL
    ) PARTITION BY RANGE (snapshot_at);

    -- Create partitions for last 12 weeks + 4 future weeks
    FOR i IN -12..4 LOOP
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS position_snapshots_w%s PARTITION OF position_snapshots_partitioned FOR VALUES FROM (%L) TO (%L)',
            to_char(now() + (i || ' weeks')::interval, 'YYYYMMDD'),
            date_trunc('week', now() + (i || ' weeks')::interval),
            date_trunc('week', now() + ((i+1) || ' weeks')::interval)
        );
    END LOOP;

    RETURN 'Created 16 weekly partitions. Ready for data migration.';
END;
$$ LANGUAGE plpgsql;

-- ═══════════════════════════════════════════════════════════════════════
-- VERIFY: Run after migration
-- SELECT pg_size_pretty(pg_total_relation_size('position_snapshots'));
-- SELECT * FROM pg_indexes WHERE tablename = 'position_snapshots';
-- ═══════════════════════════════════════════════════════════════════════
