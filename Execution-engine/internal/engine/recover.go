// Package engine — recovery protocol.
// See Wiki: concepts/state-management.md — every service MUST read snapshot →
// replay events → sync with exchange → only then subscribe to new messages.
// Target: full recovery in under 30 seconds (Phase 4 acceptance criterion).
package engine

import (
	"context"
	"fmt"
	"time"

	"github.com/Crypto-Baron/execution-engine/internal/domain"
	"github.com/Crypto-Baron/execution-engine/internal/proxy"
)

// Recover reconciles local BadgerDB state with Bybit (via bybit-proxy) before
// the engine begins accepting commands. Call this once at startup, before Start.
//
// Steps:
//  1. Load local positions/orders from BadgerDB (already loaded on demand).
//  2. Query exchange state via proxy.
//  3. For each exchange-side position not in local state → SavePosition + emit
//     events.position.opened with reason=reconcile.
//  4. For each local position not on exchange → DeletePosition + emit
//     events.position.closed with reason=reconcile.
//
// On proxy failure: the error is returned to the caller so the engine can
// decide whether to proceed (safer to abort for a trading system).
func (e *Engine) Recover(ctx context.Context) error {
	start := time.Now()
	e.logger.Info("engine: recovery — starting")

	localPositions, err := e.store.ListPositions()
	if err != nil {
		return fmt.Errorf("load local positions: %w", err)
	}
	localByS := make(map[string]*domain.TrackedPosition, len(localPositions))
	for _, p := range localPositions {
		localByS[p.Symbol] = p
	}

	exchangePositions, err := e.proxyClient.ListOpenPositions(ctx)
	if err != nil {
		return fmt.Errorf("query exchange positions: %w", err)
	}
	exchangeByS := make(map[string]proxy.OpenPosition, len(exchangePositions))
	for _, p := range exchangePositions {
		exchangeByS[p.Symbol] = p
	}

	now := time.Now().UTC()
	reconcileCount := 0

	// Case 1: exists on exchange, missing locally → adopt.
	for sym, xp := range exchangeByS {
		if _, ok := localByS[sym]; ok {
			continue
		}
		e.logger.Warn("engine: recovery — adopting exchange position not in local state",
			"symbol", sym, "side", xp.Side, "size", xp.Size,
		)
		pos := &domain.TrackedPosition{
			Symbol:     xp.Symbol,
			Side:       xp.Side,
			Size:       xp.Size,
			EntryPrice: xp.EntryPrice,
			OpenedAt:   now,
			CommandID:  "reconcile",
		}
		if err := e.store.SavePosition(pos); err != nil {
			e.logger.Error("engine: recovery — SavePosition failed", "symbol", sym, "error", err)
			continue
		}
		e.bus.PublishEvent(domain.EventPositionOpened, domain.PositionEvent{
			EventType:  domain.EventPositionOpened,
			Symbol:     pos.Symbol,
			Side:       pos.Side,
			Size:       pos.Size,
			EntryPrice: pos.EntryPrice,
			Timestamp:  now,
		})
		e.bus.PublishAlert(&domain.AlertEvent{
			AlertType: "recovery_adopt",
			Source:    "execution-engine",
			Message:   fmt.Sprintf("Adopted position %s %s %s from exchange on startup", sym, xp.Side, xp.Size),
			Severity:  "warning",
			Timestamp: now,
		})
		reconcileCount++
	}

	// Case 2: exists locally, gone from exchange → publish closed, delete.
	for sym := range localByS {
		if _, ok := exchangeByS[sym]; ok {
			continue
		}
		e.logger.Warn("engine: recovery — local position missing on exchange, closing",
			"symbol", sym,
		)
		if err := e.store.DeletePosition(sym); err != nil {
			e.logger.Error("engine: recovery — DeletePosition failed", "symbol", sym, "error", err)
		}
		e.bus.PublishEvent(domain.EventPositionClosed, domain.PositionEvent{
			EventType: domain.EventPositionClosed,
			Symbol:    sym,
			Size:      "0",
			Timestamp: now,
		})
		e.bus.PublishAlert(&domain.AlertEvent{
			AlertType: "recovery_ghost",
			Source:    "execution-engine",
			Message:   fmt.Sprintf("Local position %s not on exchange; cleared", sym),
			Severity:  "warning",
			Timestamp: now,
		})
		reconcileCount++
	}

	// Case 3: symbol matches but size/side differ → log as drift (no auto-fix).
	for sym, lp := range localByS {
		xp, ok := exchangeByS[sym]
		if !ok {
			continue
		}
		if lp.Side != xp.Side || lp.Size != xp.Size {
			e.logger.Warn("engine: recovery — position drift detected",
				"symbol", sym,
				"local_side", lp.Side, "local_size", lp.Size,
				"exchange_side", xp.Side, "exchange_size", xp.Size,
			)
			e.bus.PublishAlert(&domain.AlertEvent{
				AlertType: "recovery_drift",
				Source:    "execution-engine",
				Message: fmt.Sprintf("Position drift on %s: local=%s/%s exchange=%s/%s",
					sym, lp.Side, lp.Size, xp.Side, xp.Size),
				Severity:  "critical",
				Timestamp: now,
			})
		}
	}

	// Orders reconciliation: for now just log unknown resting orders; we don't
	// auto-cancel because they may be legitimate server-side SL/TP placed by us.
	exchangeOrders, err := e.proxyClient.ListOpenOrders(ctx)
	if err != nil {
		e.logger.Warn("engine: recovery — ListOpenOrders failed (non-fatal)", "error", err)
	} else {
		for _, o := range exchangeOrders {
			if tracked, _ := e.store.GetOrder(o.ClientOrderID); tracked == nil {
				e.logger.Warn("engine: recovery — untracked resting order on exchange",
					"order_id", o.OrderID,
					"symbol", o.Symbol,
					"status", o.Status,
				)
			}
		}
	}

	e.logger.Info("engine: recovery — complete",
		"duration", time.Since(start),
		"reconcile_events", reconcileCount,
		"local_positions", len(localByS),
		"exchange_positions", len(exchangeByS),
	)
	return nil
}
