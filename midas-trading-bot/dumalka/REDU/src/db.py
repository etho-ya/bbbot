"""
Database layer for Risk Engine — PostgreSQL via db_adapter.
v0.19.6 (2026-04-08): ML shadow predictions table (ml_predictions).
v0.19.5 (2026-04-08): Added current_sl, current_tp1, current_tp3 to position_snapshots.
v0.18.1 (2026-04-02): Added full_e_pnl, pnl_skewness to position_snapshots.
v0.13.2: 38-column signals table; 29-column position_snapshots with ML Intelligence.

All functions use pg_execute/pg_fetch_all/pg_fetch_one/pg_fetch_val
from db_adapter.py. SQL uses ? placeholders which are auto-converted
to $1, $2, ... by the adapter.

Tables:
  - signals: Every webhook signal (32 ML features + 4 shadow PnL cols + meta)
  - open_positions: Active position tracking
  - trade_outcomes: Realized PnL from closed trades
  - position_snapshots: 60s granularity ML snapshots (72K+ rows, 34 features)
    incl. btc_change_1h, rsi_14, orderbook_imbalance, long_short_ratio (v0.13.2)
    incl. current_sl, current_tp1, current_tp3 (v0.19.5)
  - ml_predictions: ML shadow mode profit predictions per position (v0.19.6)
"""
import json
import logging
from datetime import datetime, timezone

from config import config
from db_adapter import (
    init_pg_pool, close_pg_pool,
    pg_execute, pg_fetch_all, pg_fetch_one, pg_fetch_val, pg_executemany,
)

logger = logging.getLogger("risk-engine.db")


async def init_db():
    """Initialize PostgreSQL connection pool.
    Schema is already created via PG setup. No DDL needed at runtime.
    """
    await init_pg_pool()
    logger.info("PostgreSQL pool initialized (db_adapter)")


async def insert_signal(
    source: str,
    symbol: str,
    side: str,
    size: float,
    price_at_signal: float,
    volatility_used: float,
    payload_raw: dict,
    risk_request: dict,
    risk_result: dict,
    latency_ms: float,
    stop_loss: float = None,
    tp1: float = None,
    tp3: float = None,
    risk_reward: float = None,
    midas_probability: float = None,
    midas_win_rate: float = None,
    midas_trend: str = None,
    trend_strength: float = None,
    volume_level: str = None,
    is_countertrend: bool = None,
    midas_comment: str = None,
    re_annual_vol: float = None,
    re_signal_score: float = None,
    re_recommendation: str = None,
    signal_hash: str = None,
    score_components: dict = None,
    setup_master_text: str = None,
    # v0.14.2: ML feature columns (previously fetched but not persisted)
    funding_rate: float = None,
    oi_change_pct: float = None,
    market_regime: str = None,
    spread_pct: float = None,
    slippage_pct: float = None,
    multi_tf_trends_json: dict = None,
):
    """
    Persist raw Telegram signals into PostgreSQL before Risk Engine scoring.
    Sets the stage for retroactive shadow analysis.

    v0.15.8 2026-03-30: added ON CONFLICT (signal_hash) DO NOTHING for idempotency.
    """
    try:
        await pg_execute("""
            INSERT INTO signals (
                created_at, source, symbol, side, size, signal_hash,
                price_at_signal, volatility_used, payload_raw,
                risk_request, risk_result, approved, var, cvar,
                liquidation_prob, latency_ms,
                stop_loss, tp1, tp3, risk_reward,
                midas_probability, midas_win_rate, midas_trend,
                trend_strength, volume_level, is_countertrend,
                midas_comment,
                re_annual_vol, re_signal_score, re_recommendation,
                score_components, setup_master_text,
                funding_rate, oi_change_pct, market_regime,
                spread_pct, slippage_pct, multi_tf_trends_json
            ) VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?,
                ?, ?, ?,
                ?, ?,
                ?, ?, ?,
                ?, ?, ?
            )
            ON CONFLICT (signal_hash) WHERE signal_hash IS NOT NULL DO NOTHING
        """, (
            datetime.now(timezone.utc).isoformat(),
            source, symbol, side, size, signal_hash,
            price_at_signal, volatility_used,
            json.dumps(payload_raw),
            json.dumps(risk_request),
            json.dumps(risk_result),
            int(risk_result.get("approved", False)),
            risk_result.get("var"),
            risk_result.get("cvar"),
            risk_result.get("liquidation_prob"),
            latency_ms,
            stop_loss, tp1, tp3, risk_reward,
            midas_probability, midas_win_rate, midas_trend,
            trend_strength, volume_level,
            int(is_countertrend) if is_countertrend is not None else None,
            midas_comment,
            re_annual_vol, re_signal_score, re_recommendation,
            json.dumps(score_components) if score_components else None,
            setup_master_text,
            funding_rate, oi_change_pct, market_regime,
            spread_pct, slippage_pct,
            json.dumps(multi_tf_trends_json) if multi_tf_trends_json else None,
        ))
        if signal_hash:
            logger.info(f"Signal saved with hash={signal_hash} symbol={symbol}")
    except Exception as e:
        logger.error(f"Failed to insert signal into DB: {e}")


async def register_open_position(
    signal_id: int, symbol: str, side: str, size: float, entry_price: float,
    sl: float, tp1: float, tp2: float, tp3: float, score: float, rec: str,
    signal_hash: str = None,
):
    """
    Convert an approved signal into an Active Position in the database,
    handing off control to the HFT Zone Trailing tracker.
    """
    try:
        await pg_execute("""
            INSERT INTO open_positions (
                signal_id, signal_hash, opened_at, symbol, side, size, original_size,
                entry_price, current_sl, original_sl, current_tp1, current_tp2, current_tp3,
                current_price, current_pnl_pct, initial_signal_score, initial_recommendation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            signal_id, signal_hash, datetime.now(timezone.utc).isoformat(),
            symbol, side, size, size,
            entry_price, sl, sl, tp1, tp2, tp3, entry_price, 0.0, score, rec
        ))
    except Exception as e:
        logger.error(f"Failed to register open position: {e}")


async def get_recent_signals(limit: int = 50):
    """
    Fetch recent decisions generated by the GPU heuristic scorer for the dashboard UI.
    Includes score, VaR, recommendation, and timestamp.

    v0.15.9 2026-03-30: normalize size=NULL → 1.0 (SignalRecord Pydantic validation fix).
    v0.16.2 2026-03-31: LATERAL trade_outcomes → close_price + close_reason (same row).
    """
    rows = await pg_fetch_all(
        """SELECT s.*, last_close.price_at_event AS close_price,
                  last_close.event_type AS close_reason
           FROM signals s
           LEFT JOIN LATERAL (
               SELECT t.price_at_event, t.event_type
               FROM trade_outcomes t
               WHERE t.signal_hash = s.signal_hash
                 AND t.event_type IN ('full_close','sl_hit','tp3_hit','timeout',
                                      'apollo_full_exit','zone_full_exit','e_pnl_full_exit',
                                      'dumalka_close','manual_close','flip_close')
               ORDER BY t.event_at DESC
               LIMIT 1
           ) last_close ON TRUE
           ORDER BY s.id DESC LIMIT ?""", (limit,)
    )
    for d in rows:
        # Pydantic SignalRecord requires size: float — NULL breaks /signals (500 → plain text → dashboard stuck on Loading)
        if d.get("size") is None:
            d["size"] = 1.0
        if d.get("score_components"):
            try:
                d["score_components"] = json.loads(d["score_components"])
            except (json.JSONDecodeError, TypeError):
                pass
    return rows


async def get_analysis_stats():
    """Aggregate statistics for /analysis endpoint."""
    total = await pg_fetch_val(
        "SELECT COUNT(*) FROM signals WHERE COALESCE(source, '') != 'backtest_v2'"
    )

    by_rec = await pg_fetch_all("""
        SELECT re_recommendation, COUNT(*) as cnt,
               AVG(var) as avg_var, AVG(cvar) as avg_cvar,
               AVG(re_signal_score) as avg_score
        FROM signals
        WHERE re_recommendation IS NOT NULL AND COALESCE(source, '') != 'backtest_v2'
        GROUP BY re_recommendation
    """)

    ct_stats = await pg_fetch_all("""
        SELECT is_countertrend, COUNT(*) as cnt,
               AVG(var) as avg_var, AVG(cvar) as avg_cvar,
               AVG(midas_probability) as avg_midas_prob,
               AVG(re_signal_score) as avg_score
        FROM signals
        WHERE is_countertrend IS NOT NULL
        GROUP BY is_countertrend
    """)

    by_symbol = await pg_fetch_all("""
        SELECT symbol, COUNT(*) as cnt,
               AVG(var) as avg_var, AVG(cvar) as avg_cvar,
               AVG(re_signal_score) as avg_score,
               SUM(CASE WHEN re_recommendation='reject' THEN 1 ELSE 0 END) as rejected,
               SUM(CASE WHEN re_recommendation='approve' THEN 1 ELSE 0 END) as approved_cnt
        FROM signals
        WHERE COALESCE(source, '') != 'backtest_v2'
        GROUP BY symbol
        ORDER BY cnt DESC
    """)

    avg_lat = await pg_fetch_val("""
        SELECT AVG(latency_ms) FROM signals
        WHERE created_at >= datetime('now', '-24 hours')
    """)

    agreement = await pg_fetch_one("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN midas_probability >= 50 AND re_recommendation='approve' THEN 1 ELSE 0 END) as both_approve,
            SUM(CASE WHEN midas_probability < 50 AND re_recommendation='reject' THEN 1 ELSE 0 END) as both_reject,
            SUM(CASE WHEN midas_probability >= 50 AND re_recommendation='reject' THEN 1 ELSE 0 END) as midas_ok_re_reject,
            SUM(CASE WHEN midas_probability < 50 AND re_recommendation='approve' THEN 1 ELSE 0 END) as midas_bad_re_approve
        FROM signals
        WHERE midas_probability IS NOT NULL AND re_recommendation IS NOT NULL
    """)

    return {
        "total_signals": total,
        "by_recommendation": by_rec,
        "countertrend_stats": ct_stats,
        "by_symbol": by_symbol,
        "avg_latency_ms": round(avg_lat, 1) if avg_lat else None,
        "midas_vs_re_agreement": agreement or {},
    }


async def insert_trade_outcome(
    signal_hash: str = None,
    event_type: str = "unknown",
    symbol: str = "",
    side: str = None,
    price_at_event: float = None,
    pnl_pct: float = None,
    size_remaining: float = None,
    metadata: dict = None,
    re_recommendation: str = None,
    re_signal_score: float = None,
    re_var: float = None,
):
    """Record a trade outcome event (SL hit, TP hit, close, etc.)."""
    try:
        await pg_execute("""
            INSERT INTO trade_outcomes (
                signal_hash, event_type, event_at, symbol, side,
                price_at_event, pnl_pct, size_remaining, metadata,
                re_recommendation, re_signal_score, re_var, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'live')
        """, (
            signal_hash, event_type,
            datetime.now(timezone.utc).isoformat(),
            symbol, side,
            price_at_event, pnl_pct, size_remaining,
            json.dumps(metadata) if metadata else None,
            re_recommendation, re_signal_score, re_var,
        ))
        logger.info(f"Trade outcome recorded: {event_type} hash={signal_hash or 'n/a'} {symbol} pnl={pnl_pct}")
    except Exception as e:
        logger.error(f"Failed to insert trade outcome: {e}")


async def insert_re_unavailable_event(
    signal_hash: str = None,
    symbol: str = "",
    side: str = None,
    error_type: str = "unknown",
    error_message: str = "",
    retry_attempted: bool = False,
    retry_succeeded: bool = False,
    fallback_decision: str = "reduce",
    fallback_size_mult: float = 0.3,
):
    """Record an RE unavailable event for audit."""
    try:
        await pg_execute("""
            INSERT INTO re_unavailable_events (
                event_at, signal_hash, symbol, side,
                error_type, error_message,
                retry_attempted, retry_succeeded,
                fallback_decision, fallback_size_mult
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            signal_hash, symbol, side,
            error_type, error_message,
            int(retry_attempted), int(retry_succeeded),
            fallback_decision, fallback_size_mult,
        ))
        logger.warning(f"RE unavailable event recorded: {error_type} {symbol} hash={signal_hash or 'n/a'}")
    except Exception as e:
        logger.error(f"Failed to insert RE unavailable event: {e}")


async def insert_position_snapshot(
    pos_id: int, symbol: str, side: str = None,
    current_price: float = 0, entry_price: float = 0,
    pnl_pct: float = 0, max_pnl_pct: float = 0,
    drawdown_pct: float = 0, tp_progress_pct: float = 0,
    hours_open: float = 0, zone: int = 0,
    volatility: float = 0, volume_ratio: float = 1.0,
    mc_p_tp: float = 0, mc_p_sl: float = 0, mc_var: float = 0,
    full_e_pnl: float = None, pnl_skewness: float = None,
    signal_score: float = 0, signal_hash: str = None,
    action_taken: str = "hold",
    funding_rate: float = 0, oi_change_pct: float = 0,
    regime: str = None, spread_pct: float = 0,
    trend_sum: float = 0,
    btc_change_1h: float = 0, rsi_14: float = None,
    orderbook_imbalance: float = None, long_short_ratio: float = None,
    current_sl: float = None, current_tp1: float = None, current_tp3: float = None,
):
    """Record a position snapshot for ML training data.

    v0.18.1 (2026-04-02): Added full_e_pnl, pnl_skewness columns for
    E_pnl Reform analytics.
    v0.19.5 (2026-04-08): Added current_sl, current_tp1, current_tp3 —
    MC input prices for reproducibility, ML features, and SL-move audit.
    """
    try:
        await pg_execute("""
            INSERT INTO position_snapshots (
                pos_id, snapshot_at, symbol, side,
                current_price, entry_price,
                pnl_pct, max_pnl_pct, drawdown_pct, tp_progress_pct,
                hours_open, zone, volatility, volume_ratio,
                mc_p_tp, mc_p_sl, mc_var,
                full_e_pnl, pnl_skewness,
                signal_score, signal_hash, action_taken,
                funding_rate, oi_change_pct, regime, spread_pct, trend_sum,
                btc_change_1h, rsi_14, orderbook_imbalance, long_short_ratio,
                current_sl, current_tp1, current_tp3
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            pos_id, datetime.now(timezone.utc).isoformat(),
            symbol, side,
            current_price, entry_price,
            pnl_pct, max_pnl_pct, drawdown_pct, tp_progress_pct,
            hours_open, zone, volatility, volume_ratio,
            mc_p_tp, mc_p_sl, mc_var,
            full_e_pnl, pnl_skewness,
            signal_score, signal_hash, action_taken,
            funding_rate, oi_change_pct, regime, spread_pct, trend_sum,
            btc_change_1h, rsi_14, orderbook_imbalance, long_short_ratio,
            current_sl, current_tp1, current_tp3,
        ))
    except Exception as e:
        logger.error(f"Failed to insert position snapshot: {e}")


async def update_snapshot_future_pnl(pos_id: int, current_pnl_pct: float):
    """Retroactively fill future_pnl for old snapshots (1h, 4h, 12h, max_24h).

    v0.19.2 2026-04-04: Added future_pnl_12h (one-time fill at 12h age) and
    future_pnl_max_24h (running peak via GREATEST on every cycle).
    v0.8.0: Original 1h/4h implementation.

    Called every tracker cycle (~30s) with the position's current PnL.
    - 1h/4h/12h: one-time NULL fill when snapshot reaches age threshold
    - max_24h: GREATEST update on every cycle to track peak forward PnL
    """
    try:
        await pg_execute("""
            UPDATE position_snapshots
            SET future_pnl_1h = ? - pnl_pct
            WHERE pos_id = ?
              AND future_pnl_1h IS NULL
              AND snapshot_at <= datetime('now', '-1 hour')
        """, (current_pnl_pct, pos_id))

        await pg_execute("""
            UPDATE position_snapshots
            SET future_pnl_4h = ? - pnl_pct
            WHERE pos_id = ?
              AND future_pnl_4h IS NULL
              AND snapshot_at <= datetime('now', '-4 hours')
        """, (current_pnl_pct, pos_id))

        await pg_execute("""
            UPDATE position_snapshots
            SET future_pnl_12h = ? - pnl_pct
            WHERE pos_id = ?
              AND future_pnl_12h IS NULL
              AND snapshot_at <= datetime('now', '-12 hours')
        """, (current_pnl_pct, pos_id))

        # Running peak: updates ALL snapshots within 24h window on every cycle.
        # GREATEST ensures value only increases (tracks the best forward PnL).
        await pg_execute("""
            UPDATE position_snapshots
            SET future_pnl_max_24h = GREATEST(
                COALESCE(future_pnl_max_24h, -999.0),
                ? - pnl_pct
            )
            WHERE pos_id = ?
              AND snapshot_at > datetime('now', '-24 hours')
        """, (current_pnl_pct, pos_id))
    except Exception as e:
        logger.error(f"Failed to update future PnL for pos {pos_id}: {e}")


async def finalize_snapshot_future_pnl(pos_id: int, realized_pnl_pct: float,
                                      max_pnl_pct: float | None = None):
    """Fill ALL remaining NULL future_pnl for a CLOSED position.

    v0.19.2 2026-04-04: Added future_pnl_12h finalization and future_pnl_max_24h
    final pass using max_pnl_pct (position's all-time peak) as upper bound.
    v0.18.7: Original 1h/4h finalization.

    When a position closes, realized_pnl is the ground truth for all snapshots
    that couldn't compute future_pnl due to the time constraint.
    max_pnl_pct provides the best-case forward PnL for future_pnl_max_24h.
    """
    if realized_pnl_pct is None:
        return
    try:
        await pg_execute("""
            UPDATE position_snapshots
            SET future_pnl_1h = ? - pnl_pct
            WHERE pos_id = ?
              AND future_pnl_1h IS NULL
        """, (realized_pnl_pct, pos_id))

        await pg_execute("""
            UPDATE position_snapshots
            SET future_pnl_4h = ? - pnl_pct
            WHERE pos_id = ?
              AND future_pnl_4h IS NULL
        """, (realized_pnl_pct, pos_id))

        await pg_execute("""
            UPDATE position_snapshots
            SET future_pnl_12h = ? - pnl_pct
            WHERE pos_id = ?
              AND future_pnl_12h IS NULL
        """, (realized_pnl_pct, pos_id))

        # Final peak pass: use max_pnl_pct if available (all-time peak during trade).
        # GREATEST ensures we don't overwrite a higher live-tracked value.
        peak = max_pnl_pct if max_pnl_pct is not None else realized_pnl_pct
        await pg_execute("""
            UPDATE position_snapshots
            SET future_pnl_max_24h = GREATEST(
                COALESCE(future_pnl_max_24h, -999.0),
                ? - pnl_pct
            )
            WHERE pos_id = ?
              AND future_pnl_max_24h IS NULL
        """, (peak, pos_id))
    except Exception as e:
        logger.error(f"Failed to finalize future PnL for pos {pos_id}: {e}")


async def get_effectiveness_stats():
    """Analyze RE recommendation effectiveness by linking signals → outcomes."""
    total_outcomes = await pg_fetch_val("SELECT COUNT(*) FROM trade_outcomes WHERE source='live'")

    by_event_type = await pg_fetch_all("""
        SELECT event_type, COUNT(*) as cnt, AVG(pnl_pct) as avg_pnl
        FROM trade_outcomes WHERE source='live' GROUP BY event_type ORDER BY cnt DESC
    """)

    by_recommendation = await pg_fetch_all("""
        SELECT
            s.re_recommendation,
            COUNT(DISTINCT t.signal_hash) as trades,
            AVG(t.pnl_pct) as avg_pnl,
            MIN(t.pnl_pct) as worst_pnl,
            MAX(t.pnl_pct) as best_pnl,
            SUM(CASE WHEN t.pnl_pct > 0 THEN 1 ELSE 0 END) as profitable,
            SUM(CASE WHEN t.pnl_pct <= 0 THEN 1 ELSE 0 END) as unprofitable
        FROM trade_outcomes t
        JOIN signals s ON s.signal_hash = t.signal_hash
        WHERE t.event_type IN ('full_close', 'sl_hit', 'tp3_hit', 'timeout', 'apollo_full_exit', 'zone_full_exit', 'e_pnl_full_exit', 'dumalka_close', 'manual_close', 'flip_close')
          AND t.signal_hash IS NOT NULL AND s.signal_hash IS NOT NULL
        GROUP BY s.re_recommendation
    """)

    by_score_bucket = await pg_fetch_all("""
        SELECT
            CASE
                WHEN s.re_signal_score < 0.35 THEN '0.00-0.35 (reject)'
                WHEN s.re_signal_score < 0.50 THEN '0.35-0.50 (reduce)'
                WHEN s.re_signal_score < 0.70 THEN '0.50-0.70 (approve)'
                ELSE '0.70+ (strong approve)'
            END as score_bucket,
            COUNT(DISTINCT t.signal_hash) as trades,
            AVG(t.pnl_pct) as avg_pnl,
            SUM(CASE WHEN t.pnl_pct > 0 THEN 1 ELSE 0 END) as profitable
        FROM trade_outcomes t
        JOIN signals s ON s.signal_hash = t.signal_hash
        WHERE t.event_type IN ('full_close', 'sl_hit', 'tp3_hit', 'timeout', 'apollo_full_exit', 'zone_full_exit', 'e_pnl_full_exit', 'dumalka_close', 'manual_close', 'flip_close')
          AND t.signal_hash IS NOT NULL AND s.signal_hash IS NOT NULL
          AND s.re_signal_score IS NOT NULL
        GROUP BY score_bucket ORDER BY score_bucket
    """)

    countertrend_effectiveness = await pg_fetch_all("""
        SELECT
            s.is_countertrend,
            COUNT(DISTINCT t.signal_hash) as trades,
            AVG(t.pnl_pct) as avg_pnl,
            SUM(CASE WHEN t.pnl_pct > 0 THEN 1 ELSE 0 END) as profitable
        FROM trade_outcomes t
        JOIN signals s ON s.signal_hash = t.signal_hash
        WHERE t.event_type IN ('full_close', 'sl_hit', 'tp3_hit', 'timeout', 'apollo_full_exit', 'zone_full_exit', 'e_pnl_full_exit', 'dumalka_close', 'manual_close', 'flip_close')
          AND t.signal_hash IS NOT NULL AND s.signal_hash IS NOT NULL
        GROUP BY s.is_countertrend
    """)

    return {
        "total_outcomes": total_outcomes,
        "by_event_type": by_event_type,
        "effectiveness_by_recommendation": by_recommendation,
        "effectiveness_by_score_bucket": by_score_bucket,
        "countertrend_effectiveness": countertrend_effectiveness,
    }


async def get_pending_opportunity_costs():
    """Fetch closed trades from trade_outcomes that need MFE/MAE calculation (closed > 4 hours ago)."""
    return await pg_fetch_all("""
        SELECT
            t.signal_hash, t.symbol, t.side, t.event_at as closed_at,
            t.price_at_event as close_price, t.event_type as close_reason
        FROM trade_outcomes t
        WHERE t.event_type IN ('full_close', 'sl_hit', 'tp3_hit', 'timeout', 'apollo_full_exit', 'zone_full_exit', 'e_pnl_full_exit', 'dumalka_close', 'manual_close', 'flip_close')
          AND t.event_at <= NOW() - INTERVAL '4 hours'
          AND t.signal_hash IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM trade_opportunity_cost o WHERE o.signal_hash = t.signal_hash
          )
    """)


async def insert_opportunity_cost(
    signal_hash: str, symbol: str, side: str, closed_at: str,
    close_price: float, close_reason: str,
    mfe_1h: float, mfe_4h: float, mae_1h: float, mae_4h: float
):
    """Save MFE/MAE metrics into trade_opportunity_cost."""
    try:
        await pg_execute("""
            INSERT INTO trade_opportunity_cost (
                signal_hash, symbol, side, closed_at, close_price, close_reason,
                mfe_1h, mfe_4h, mae_1h, mae_4h
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (signal_hash) DO NOTHING
        """, (
            signal_hash, symbol, side, closed_at, close_price, close_reason,
            mfe_1h, mfe_4h, mae_1h, mae_4h
        ))
    except Exception as e:
        logger.error(f"Failed to insert opportunity cost for {signal_hash}: {e}")
