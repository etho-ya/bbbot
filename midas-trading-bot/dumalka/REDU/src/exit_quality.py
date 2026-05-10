"""
Exit Decision Quality Tracker — analytical module.
v0.19.2 2026-04-04: Added max_pnl_24h / max_pnl_24h_hour peak tracking in what_if_outcomes.
  Piggybacks on existing 24-candle kline loop — zero extra API calls.
  Enables precise "missed rocket" analytics for ML training.
v0.18.5 2026-04-03: Fixed backfill pipeline — SWAP-first OKX, sentinel rows
                     for unprocessable positions, removed close_reason whitelist,
                     DESC ordering (newest first), increased throughput.
v0.18.0 2026-04-01: Initial implementation.

Evaluates the quality of ALL Dumalka exit decisions for training purposes:
  Phase 1 — "Closed too early?": post-close SL/TP sim + multi-horizon PnL + peak tracking
  Phase 2 — "Did sl_breakeven work?": BE effectiveness by close_reason
  Phase 3 — "Why didn't Dumalka close?": sl_hit positions with leaked profit

Architecture:
  - Pure analytics, zero impact on trading logic.
  - Sole writer to `what_if_outcomes` via incremental backfill loop.
  - Read-only queries for Phases 2-3 (data already in position_snapshots/open_positions).
  - Klines via kline_fetcher.py (no circular import).

Tables used:
  - what_if_outcomes  (WRITE — Phase 1 backfill)
  - open_positions    (READ)
  - position_snapshots (READ — Phases 2-3)
"""
import asyncio
import logging
import time
from datetime import datetime, timezone

from db_adapter import pg_fetch_all, pg_fetch_one, pg_execute, pg_executemany
from kline_fetcher import fetch_klines_with_fallbacks

logger = logging.getLogger("risk-engine.exit_quality")

EXIT_QUALITY_CYCLE_SEC = 300   # 5 min between backfill runs (v0.18.5: was 900)
EXIT_QUALITY_BATCH_SIZE = 50   # positions per cycle (v0.18.5: was 10)

_heartbeat: dict = {}  # {"last_success": epoch, "analyzed": N} — polled by /health


# ---------------------------------------------------------------------------
# Phase 1A: Core analysis — single position
# ---------------------------------------------------------------------------

def analyze_position_exit(pos: dict, klines: list) -> dict | None:
    """
    Analyze a single closed position against 24h of post-close klines.

    Returns dict ready for INSERT into what_if_outcomes, or None on bad data.

    Computes:
      - SL/TP simulation (would_hit_tp / would_hit_sl / neither_24h / no_sl_tp)
      - Multi-horizon PnL (1h, 4h, 12h, 24h after close)
      - NULL realized_pnl fallback (trend_reversal positions)

    v0.18.0 2026-04-01: initial implementation.
    """
    entry = float(pos["entry_price"]) if pos["entry_price"] else 0.0
    exit_price = float(pos["current_price"]) if pos["current_price"] else 0.0
    side = pos["side"]

    if entry <= 0 or exit_price <= 0:
        return None

    exit_pnl = pos.get("realized_pnl_pct")
    if exit_pnl is None and entry > 0 and exit_price > 0:
        if side == "long":
            exit_pnl = ((exit_price - entry) / entry) * 100
        else:
            exit_pnl = ((entry - exit_price) / entry) * 100

    sl = float(pos["current_sl"]) if pos.get("current_sl") else 0.0
    tp3 = float(pos["current_tp3"]) if pos.get("current_tp3") else 0.0
    tp1 = float(pos["current_tp1"]) if pos.get("current_tp1") else 0.0
    tp = tp3 if tp3 > 0 else tp1

    close_reason = pos.get("close_reason_detailed") or pos.get("close_reason") or "unknown"

    # --- SL/TP simulation ---
    outcome = "no_sl_tp" if (sl <= 0 or tp <= 0) else "neither_24h"
    outcome_price = None
    hours_to_outcome = None

    if sl > 0 and tp > 0:
        for i, k in enumerate(klines):
            try:
                high = float(k[2])
                low = float(k[3])
            except (IndexError, TypeError, ValueError):
                continue

            if side == "long":
                if low <= sl:
                    outcome = "would_hit_sl"
                    outcome_price = sl
                    hours_to_outcome = i + 1
                    break
                if high >= tp:
                    outcome = "would_hit_tp"
                    outcome_price = tp
                    hours_to_outcome = i + 1
                    break
            else:
                if high >= sl:
                    outcome = "would_hit_sl"
                    outcome_price = sl
                    hours_to_outcome = i + 1
                    break
                if low <= tp:
                    outcome = "would_hit_tp"
                    outcome_price = tp
                    hours_to_outcome = i + 1
                    break

    missed_pnl = 0.0
    if outcome_price and entry > 0:
        if side == "long":
            missed_pnl = ((outcome_price - exit_price) / entry) * 100
        else:
            missed_pnl = ((exit_price - outcome_price) / entry) * 100

    # --- Multi-horizon PnL ---
    def _horizon_pnl(kline_idx: int) -> float | None:
        if kline_idx >= len(klines):
            return None
        try:
            close_px = float(klines[kline_idx][4])
        except (IndexError, TypeError, ValueError):
            return None
        if side == "long":
            return round(((close_px - exit_price) / entry) * 100, 4)
        return round(((exit_price - close_px) / entry) * 100, 4)

    pnl_1h = _horizon_pnl(0)    # 1st hourly candle close
    pnl_4h = _horizon_pnl(3)    # 4th
    pnl_12h = _horizon_pnl(11)  # 12th
    pnl_24h = _horizon_pnl(23)  # 24th

    # v0.19.2: Peak favorable PnL within 24h post-close
    max_favorable = 0.0
    max_favorable_hour = 0
    for i, k in enumerate(klines):
        try:
            high = float(k[2])
            low = float(k[3])
        except (IndexError, TypeError, ValueError):
            continue
        if side == "long":
            pnl_at_best = ((high - exit_price) / entry) * 100
        else:
            pnl_at_best = ((exit_price - low) / entry) * 100
        if pnl_at_best > max_favorable:
            max_favorable = pnl_at_best
            max_favorable_hour = i + 1

    return {
        "pos_id": pos["id"],
        "symbol": pos["symbol"],
        "side": side,
        "close_reason": close_reason,
        "exit_pnl": round(exit_pnl, 2) if exit_pnl is not None else None,
        "what_if": outcome,
        "missed_pnl": round(missed_pnl, 2),
        "hours_to_outcome": hours_to_outcome,
        "pnl_1h_after": pnl_1h,
        "pnl_4h_after": pnl_4h,
        "pnl_12h_after": pnl_12h,
        "pnl_24h_after": pnl_24h,
        "max_pnl_24h": round(max_favorable, 4),
        "max_pnl_24h_hour": max_favorable_hour if max_favorable > 0 else None,
    }


# ---------------------------------------------------------------------------
# Phase 1B: Sentinel for unprocessable positions (v0.18.5)
# ---------------------------------------------------------------------------

async def _mark_data_unavailable(pos: dict, reason: str):
    """Insert a sentinel row so the position is excluded from future batches."""
    close_reason = pos.get("close_reason_detailed") or pos.get("close_reason") or "unknown"
    await pg_execute("""
        INSERT INTO what_if_outcomes (pos_id, symbol, side, close_reason, what_if, analyzed_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (pos_id) DO NOTHING
    """, (pos["id"], pos["symbol"], pos["side"], close_reason,
          f"data_unavailable:{reason}", datetime.now(timezone.utc)))


# ---------------------------------------------------------------------------
# Phase 1D: Background backfill loop (sole writer to what_if_outcomes)
# ---------------------------------------------------------------------------

async def exit_quality_backfill_loop():
    """
    Incremental background loop — analyzes closed positions post-close.

    Sole writer to what_if_outcomes. Uses ON CONFLICT for idempotent upsert.
    Kline drift sanity check rejects stale data (>24h drift) and inserts
    a sentinel row so the position is not re-fetched.

    Processes ALL close reasons (no whitelist). Newest-first ordering ensures
    recent positions (with available klines) are analyzed before old ones.

    Cycle: every EXIT_QUALITY_CYCLE_SEC (5 min).
    Batch: EXIT_QUALITY_BATCH_SIZE (50) positions per cycle.

    v0.18.0 2026-04-01: initial implementation.
    v0.18.5 2026-04-03: removed close_reason whitelist, DESC ordering,
                         sentinel rows, increased batch/cycle throughput.
    """
    await asyncio.sleep(45)
    logger.info("📊 Exit Quality backfill loop started (v0.18.5)")

    while True:
        try:
            positions = await pg_fetch_all("""
                SELECT id, symbol, side, entry_price, current_price,
                       current_sl, current_tp1, current_tp3, closed_at,
                       close_reason, close_reason_detailed, realized_pnl_pct
                FROM open_positions
                WHERE status = 'closed'
                  AND closed_at IS NOT NULL
                  AND closed_at < NOW() - INTERVAL '1 hour'
                  AND id NOT IN (SELECT pos_id FROM what_if_outcomes)
                ORDER BY closed_at DESC
                LIMIT %s
            """, (EXIT_QUALITY_BATCH_SIZE,))

            if not positions:
                _heartbeat["last_success"] = time.time()
                _heartbeat["analyzed"] = 0
                await asyncio.sleep(EXIT_QUALITY_CYCLE_SEC)
                continue

            logger.info(
                f"📊 ExitQ: batch of {len(positions)} positions "
                f"(newest: {positions[0].get('closed_at')}, "
                f"oldest: {positions[-1].get('closed_at')})"
            )
            analyzed = 0
            for pos in positions:
                try:
                    closed_at_val = pos["closed_at"]
                    if isinstance(closed_at_val, str):
                        closed_dt = datetime.fromisoformat(closed_at_val.replace("Z", "+00:00"))
                    else:
                        closed_dt = closed_at_val
                    if closed_dt.tzinfo is None:
                        closed_dt = closed_dt.replace(tzinfo=timezone.utc)

                    close_ts = int(closed_dt.timestamp() * 1000)

                    klines = await fetch_klines_with_fallbacks(
                        pos["symbol"], close_ts, 24, "60", "1H"
                    )
                    if not klines:
                        logger.debug(f"📊 ExitQ: no klines for {pos['symbol']} pos={pos['id']}, marking unavailable")
                        await _mark_data_unavailable(pos, "no_klines")
                        continue

                    first_kline_ts = int(klines[0][0])
                    drift_ms = abs(first_kline_ts - close_ts)
                    if drift_ms > 24 * 3600 * 1000:
                        logger.warning(
                            f"📊 ExitQ: kline drift {drift_ms/3600000:.1f}h for "
                            f"{pos['symbol']} pos={pos['id']}, marking unavailable"
                        )
                        await _mark_data_unavailable(pos, "kline_drift")
                        continue

                    result = analyze_position_exit(pos, klines)
                    if not result:
                        await _mark_data_unavailable(pos, "bad_data")
                        continue

                    now = datetime.now(timezone.utc)
                    await pg_execute("""
                        INSERT INTO what_if_outcomes
                        (pos_id, symbol, side, close_reason, exit_pnl, what_if,
                         missed_pnl, hours_to_outcome, analyzed_at,
                         pnl_1h_after, pnl_4h_after, pnl_12h_after, pnl_24h_after,
                         max_pnl_24h, max_pnl_24h_hour)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (pos_id) DO UPDATE SET
                            symbol=EXCLUDED.symbol, side=EXCLUDED.side,
                            close_reason=EXCLUDED.close_reason, exit_pnl=EXCLUDED.exit_pnl,
                            what_if=EXCLUDED.what_if, missed_pnl=EXCLUDED.missed_pnl,
                            hours_to_outcome=EXCLUDED.hours_to_outcome,
                            analyzed_at=EXCLUDED.analyzed_at,
                            pnl_1h_after=EXCLUDED.pnl_1h_after,
                            pnl_4h_after=EXCLUDED.pnl_4h_after,
                            pnl_12h_after=EXCLUDED.pnl_12h_after,
                            pnl_24h_after=EXCLUDED.pnl_24h_after,
                            max_pnl_24h=EXCLUDED.max_pnl_24h,
                            max_pnl_24h_hour=EXCLUDED.max_pnl_24h_hour
                    """, (
                        result["pos_id"], result["symbol"], result["side"],
                        result["close_reason"], result["exit_pnl"],
                        result["what_if"], result["missed_pnl"],
                        result["hours_to_outcome"], now,
                        result["pnl_1h_after"], result["pnl_4h_after"],
                        result["pnl_12h_after"], result["pnl_24h_after"],
                        result["max_pnl_24h"], result["max_pnl_24h_hour"],
                    ))
                    analyzed += 1

                except Exception as e:
                    logger.error(f"📊 ExitQ: error analyzing pos={pos.get('id')}: {e}")
                    continue

            _heartbeat["last_success"] = time.time()
            _heartbeat["analyzed"] = analyzed
            if analyzed > 0:
                logger.info(f"📊 ExitQ backfill: analyzed {analyzed}/{len(positions)} positions")

        except Exception as e:
            _heartbeat["last_error"] = str(e)
            logger.error(f"📊 ExitQ backfill loop error: {e}")

        await asyncio.sleep(EXIT_QUALITY_CYCLE_SEC)


# ---------------------------------------------------------------------------
# Phase 1: API — summary + recalculate
# ---------------------------------------------------------------------------

async def get_exit_quality_summary(since: str = None) -> dict:
    """
    Read-only aggregation of what_if_outcomes by close_reason.

    Returns per-reason stats: count, avg exit PnL, avg multi-horizon PnL,
    premature exit rate (pnl_4h_after > 0 means market kept going up after close).
    Includes data maturity indicators (earliest/latest analyzed_at per reason).

    v0.18.0 2026-04-01: initial implementation.
    """
    since_clause = ""
    params = ()
    if since:
        since_clause = "WHERE w.analyzed_at >= %s"
        params = (since,)

    rows = await pg_fetch_all(f"""
        SELECT
            w.close_reason,
            COUNT(*) as cnt,
            ROUND(AVG(w.exit_pnl)::numeric, 2) as avg_exit_pnl,
            ROUND(AVG(w.pnl_1h_after)::numeric, 2) as avg_pnl_1h,
            ROUND(AVG(w.pnl_4h_after)::numeric, 2) as avg_pnl_4h,
            ROUND(AVG(w.pnl_12h_after)::numeric, 2) as avg_pnl_12h,
            ROUND(AVG(w.pnl_24h_after)::numeric, 2) as avg_pnl_24h,
            COUNT(*) FILTER (WHERE w.pnl_4h_after > 0) as premature_exit_cnt,
            ROUND(AVG(w.missed_pnl)::numeric, 2) as avg_missed_pnl,
            COUNT(*) FILTER (WHERE w.what_if = 'would_hit_tp') as would_tp,
            COUNT(*) FILTER (WHERE w.what_if = 'would_hit_sl') as would_sl,
            COUNT(*) FILTER (WHERE w.what_if = 'neither_24h') as neither,
            MIN(w.analyzed_at) as first_analyzed,
            MAX(w.analyzed_at) as last_analyzed
        FROM what_if_outcomes w
        {since_clause}
        GROUP BY w.close_reason
        ORDER BY cnt DESC
    """, params)

    total = await pg_fetch_one(f"""
        SELECT COUNT(*) as total,
               ROUND(AVG(exit_pnl)::numeric, 2) as avg_exit_pnl,
               ROUND(AVG(pnl_4h_after)::numeric, 2) as avg_pnl_4h,
               COUNT(*) FILTER (WHERE pnl_4h_after > 0) as premature_cnt
        FROM what_if_outcomes
        {"WHERE analyzed_at >= %s" if since else ""}
    """, params)

    return {
        "by_close_reason": [dict(r) for r in rows] if rows else [],
        "totals": dict(total) if total else {},
        "since_filter": since,
    }


async def recalculate_all_exits() -> dict:
    """
    Manual full recalculation: DELETE all what_if_outcomes, then let the backfill
    loop re-analyze everything incrementally. Returns count of eligible positions.

    v0.18.0 2026-04-01: initial implementation.
    v0.18.5 2026-04-03: removed close_reason whitelist — all closed positions eligible.
    """
    await pg_execute("DELETE FROM what_if_outcomes")
    eligible = await pg_fetch_one("""
        SELECT COUNT(*) as cnt FROM open_positions
        WHERE status = 'closed'
          AND closed_at IS NOT NULL
          AND closed_at < NOW() - INTERVAL '1 hour'
    """)
    cnt = eligible["cnt"] if eligible else 0
    logger.info(f"📊 ExitQ: recalculate triggered — cleared all, {cnt} positions eligible for re-analysis")
    return {"cleared": True, "eligible_positions": cnt, "message": f"Backfill loop will re-analyze {cnt} positions over next cycles"}


# ---------------------------------------------------------------------------
# Phase 2: sl_breakeven effectiveness (read-only)
# ---------------------------------------------------------------------------

async def sl_breakeven_effectiveness(since: str = None) -> dict:
    """
    Analyze sl_breakeven action effectiveness from position_snapshots.

    For each closed position that had a sl_breakeven action:
      - What was the final close_reason?
      - Did BE save money (stopped near 0) or kill upside (position was profitable after)?

    Uses pos_id (NOT position_id — confirmed in schema).

    v0.18.0 2026-04-01: initial implementation.
    """
    since_clause = ""
    params = ()
    if since:
        since_clause = "AND ps.snapshot_at >= %s"
        params = (since,)

    by_reason = await pg_fetch_all(f"""
        SELECT
            op.close_reason,
            COUNT(DISTINCT ps.pos_id) as positions_with_be,
            COUNT(DISTINCT ps.pos_id) FILTER (
                WHERE op.realized_pnl_pct BETWEEN -0.5 AND 0.5
            ) as stopped_near_be,
            COUNT(DISTINCT ps.pos_id) FILTER (
                WHERE op.realized_pnl_pct > 0.5
            ) as profitable_after,
            COUNT(DISTINCT ps.pos_id) FILTER (
                WHERE op.realized_pnl_pct < -0.5
            ) as loss_after,
            ROUND(AVG(op.realized_pnl_pct)::numeric, 2) as avg_final_pnl
        FROM position_snapshots ps
        JOIN open_positions op ON ps.pos_id = op.id
        WHERE ps.action_taken = 'sl_breakeven'
          AND op.status = 'closed'
          {since_clause}
        GROUP BY op.close_reason
        ORDER BY positions_with_be DESC
    """, params)

    total = await pg_fetch_one(f"""
        SELECT
            COUNT(DISTINCT ps.pos_id) as total_with_be,
            ROUND(AVG(op.realized_pnl_pct)::numeric, 2) as avg_pnl,
            COUNT(DISTINCT ps.pos_id) FILTER (
                WHERE op.realized_pnl_pct BETWEEN -0.5 AND 0.5
            ) as stopped_near_be,
            COUNT(DISTINCT ps.pos_id) FILTER (
                WHERE op.realized_pnl_pct > 0.5
            ) as profitable_after,
            MIN(ps.snapshot_at) as first_be,
            MAX(ps.snapshot_at) as last_be
        FROM position_snapshots ps
        JOIN open_positions op ON ps.pos_id = op.id
        WHERE ps.action_taken = 'sl_breakeven'
          AND op.status = 'closed'
          {since_clause}
    """, params)

    return {
        "by_close_reason": [dict(r) for r in by_reason] if by_reason else [],
        "totals": dict(total) if total else {},
        "since_filter": since,
    }


# ---------------------------------------------------------------------------
# Phase 3: Missed exit analysis — sl_hit positions (read-only)
# ---------------------------------------------------------------------------

async def missed_exit_analysis(since: str = None) -> dict:
    """
    Analyze sl_hit positions where Dumalka did NOT close in time.

    For each non-manual sl_hit with max_pnl > 0:
      - Zone at peak PnL (from position_snapshots)
      - Action taken at peak (hold, sl_breakeven, etc.)
      - MC probabilities at peak (mc_p_tp, mc_p_sl)
      - Leaked PnL (max_pnl - realized_pnl)
      - Time from peak to SL hit

    Training value: identifies whether Dumalka's "hold" decisions at peak
    were correct, and whether zone thresholds need recalibration.

    v0.18.0 2026-04-01: initial implementation.
    """
    since_clause = ""
    params = ()
    if since:
        since_clause = "AND op.closed_at >= %s"
        params = (since,)

    positions = await pg_fetch_all(f"""
        SELECT
            op.id, op.symbol, op.side, op.max_pnl_pct, op.realized_pnl_pct,
            ROUND((op.max_pnl_pct - COALESCE(op.realized_pnl_pct, 0))::numeric, 2) as leaked_pnl,
            ps_peak.zone as zone_at_peak,
            ps_peak.action_taken as action_at_peak,
            ps_peak.mc_p_tp, ps_peak.mc_p_sl,
            ps_peak.drawdown_pct as dd_at_peak,
            ROUND(EXTRACT(EPOCH FROM (op.closed_at - ps_peak.snapshot_at))::numeric / 3600, 1) as hours_peak_to_sl,
            op.closed_at
        FROM open_positions op
        JOIN LATERAL (
            SELECT zone, action_taken, pnl_pct, mc_p_tp, mc_p_sl,
                   drawdown_pct, snapshot_at
            FROM position_snapshots
            WHERE pos_id = op.id
            ORDER BY pnl_pct DESC NULLS LAST
            LIMIT 1
        ) ps_peak ON true
        WHERE op.status = 'closed'
          AND op.close_reason = 'sl_hit'
          AND op.close_reason_detailed NOT LIKE 'manual_close_%%'
          AND op.max_pnl_pct > 0
          {since_clause}
        ORDER BY leaked_pnl DESC
    """, params)

    by_zone = await pg_fetch_all(f"""
        SELECT
            ps_peak.zone as zone_at_peak,
            COUNT(*) as positions,
            ROUND(AVG(op.max_pnl_pct)::numeric, 2) as avg_max_pnl,
            ROUND(AVG(op.realized_pnl_pct)::numeric, 2) as avg_realized,
            ROUND(AVG(op.max_pnl_pct - COALESCE(op.realized_pnl_pct, 0))::numeric, 2) as avg_leaked,
            ROUND(SUM(op.max_pnl_pct - COALESCE(op.realized_pnl_pct, 0))::numeric, 1) as total_leaked,
            ROUND(AVG(ps_peak.mc_p_tp)::numeric, 3) as avg_mc_p_tp_at_peak,
            ROUND(AVG(ps_peak.mc_p_sl)::numeric, 3) as avg_mc_p_sl_at_peak,
            ROUND(AVG(EXTRACT(EPOCH FROM (op.closed_at - ps_peak.snapshot_at)) / 3600)::numeric, 1) as avg_hours_peak_to_sl
        FROM open_positions op
        JOIN LATERAL (
            SELECT zone, pnl_pct, mc_p_tp, mc_p_sl, snapshot_at
            FROM position_snapshots
            WHERE pos_id = op.id
            ORDER BY pnl_pct DESC NULLS LAST
            LIMIT 1
        ) ps_peak ON true
        WHERE op.status = 'closed'
          AND op.close_reason = 'sl_hit'
          AND op.close_reason_detailed NOT LIKE 'manual_close_%%'
          AND op.max_pnl_pct > 0
          {since_clause}
        GROUP BY ps_peak.zone
        ORDER BY ps_peak.zone
    """, params)

    since_clause_op = ""
    if since:
        since_clause_op = "AND closed_at >= %s"

    totals = await pg_fetch_one(f"""
        SELECT
            COUNT(*) as total_sl_hit,
            COUNT(*) FILTER (WHERE max_pnl_pct > 0) as had_profit,
            COUNT(*) FILTER (WHERE max_pnl_pct > 1.0) as had_profit_1pct,
            ROUND(SUM(CASE WHEN max_pnl_pct > 0 THEN max_pnl_pct - COALESCE(realized_pnl_pct, 0) ELSE 0 END)::numeric, 1) as total_leaked,
            ROUND(AVG(realized_pnl_pct)::numeric, 2) as avg_realized_pnl,
            ROUND(SUM(realized_pnl_pct)::numeric, 1) as total_realized_pnl
        FROM open_positions
        WHERE status = 'closed'
          AND close_reason = 'sl_hit'
          AND close_reason_detailed NOT LIKE 'manual_close_%%'
          {since_clause_op}
    """, params)

    return {
        "positions": [dict(p) for p in positions] if positions else [],
        "by_zone": [dict(z) for z in by_zone] if by_zone else [],
        "totals": dict(totals) if totals else {},
        "since_filter": since,
        "training_insights": _generate_missed_exit_insights(
            [dict(z) for z in by_zone] if by_zone else [],
            dict(totals) if totals else {},
        ),
    }


def _generate_missed_exit_insights(by_zone: list, totals: dict) -> list:
    """
    Auto-generate training insights from missed exit data.

    v0.18.0 2026-04-01: initial implementation.
    """
    insights = []

    zone1 = next((z for z in by_zone if z.get("zone_at_peak") == 1), None)
    if zone1 and zone1.get("positions", 0) >= 5:
        insights.append({
            "severity": "high",
            "insight": (
                f"Zone 1 BE failure: {zone1['positions']} positions reached Zone 1 "
                f"(BE threshold) but still hit SL. Avg leaked: {zone1.get('avg_leaked', 0)}%. "
                f"Consider reviewing DD threshold (currently 40%)."
            ),
        })

    zone2 = next((z for z in by_zone if z.get("zone_at_peak") == 2), None)
    if zone2 and zone2.get("positions", 0) >= 3:
        insights.append({
            "severity": "high",
            "insight": (
                f"Zone 2 partial close leak: {zone2['positions']} positions reached Zone 2 "
                f"but still hit SL. Avg leaked: {zone2.get('avg_leaked', 0)}%. "
                f"Partial close may not be aggressive enough."
            ),
        })

    for z in by_zone:
        mc_tp = z.get("avg_mc_p_tp_at_peak")
        mc_sl = z.get("avg_mc_p_sl_at_peak")
        if mc_tp and mc_sl and mc_tp > mc_sl and z.get("positions", 0) >= 3:
            insights.append({
                "severity": "medium",
                "insight": (
                    f"MC miscalibration at Zone {z['zone_at_peak']}: "
                    f"MC predicted TP (p_tp={mc_tp}) > SL (p_sl={mc_sl}) at peak, "
                    f"but {z['positions']} positions hit SL anyway."
                ),
            })
            break

    total_leaked = totals.get("total_leaked", 0)
    if total_leaked and total_leaked > 50:
        insights.append({
            "severity": "critical",
            "insight": (
                f"Total leaked profit from sl_hit positions: {total_leaked}%. "
                f"This exceeds the combined profit from Dumalka active closes."
            ),
        })

    return insights
