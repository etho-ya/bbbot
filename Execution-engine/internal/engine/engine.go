// Package engine coordinates the execution flow: receiving commands,
// validating them, checking risk, and proxying to Bybit.
package engine

import (
	"context"
	"fmt"
	"log/slog"
	"sync/atomic"
	"time"

	"github.com/Crypto-Baron/execution-engine/internal/config"
	"github.com/Crypto-Baron/execution-engine/internal/domain"
	"github.com/Crypto-Baron/execution-engine/internal/metrics"
	"github.com/Crypto-Baron/execution-engine/internal/proxy"
	"github.com/Crypto-Baron/execution-engine/internal/risk"
	"github.com/Crypto-Baron/execution-engine/internal/state"
	"github.com/Crypto-Baron/execution-engine/internal/transport"
)

// Engine is the core application logic.
type Engine struct {
	cfg         *config.Config
	store       *state.Store
	bus         *transport.Bus
	proxyClient *proxy.Client
	gate        *risk.Gate
	deadman     *DeadManSwitch
	hmacSecret  []byte
	logger      *slog.Logger

	killed   atomic.Bool      // set true after alerts.kill — all new commands rejected
	rootCtx  context.Context  // captured in Start() for use by kill handler
}

// New creates a new Engine.
func New(
	cfg *config.Config,
	store *state.Store,
	bus *transport.Bus,
	proxyClient *proxy.Client,
	gate *risk.Gate,
	deadman *DeadManSwitch,
	hmacSecret []byte,
	logger *slog.Logger,
) *Engine {
	return &Engine{
		cfg:         cfg,
		store:       store,
		bus:         bus,
		proxyClient: proxyClient,
		gate:        gate,
		deadman:     deadman,
		hmacSecret:  hmacSecret,
		logger:      logger,
	}
}

// Start subscribes to NATS and begins processing.
func (e *Engine) Start(ctx context.Context) error {
	e.logger.Info("engine: starting up")
	e.rootCtx = ctx

	// Start dead-man switch in background
	go e.deadman.Run(ctx)

	// Subscribe to brain heartbeats
	if err := e.bus.SubscribeBrainHeartbeat(e.deadman.RecordHeartbeat); err != nil {
		return fmt.Errorf("subscribe heartbeat: %w", err)
	}

	// Subscribe to kill signals
	if err := e.bus.SubscribeKillSignal(e.handleKillSignal); err != nil {
		return fmt.Errorf("subscribe kill: %w", err)
	}

	// Subscribe to commands
	if err := e.bus.SubscribeCommands(e.handleCommand); err != nil {
		return fmt.Errorf("subscribe commands: %w", err)
	}

	// Start execution heartbeat loop
	go e.heartbeatLoop(ctx)

	return nil
}

// heartbeatLoop publishes the engine's heartbeat periodically and refreshes
// gauge metrics that don't have a natural event-driven update site.
func (e *Engine) heartbeatLoop(ctx context.Context) {
	ticker := time.NewTicker(10 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if err := e.bus.PublishHeartbeat(); err != nil {
				e.logger.Error("engine: failed to publish heartbeat", "error", err)
			}
			// Refresh gauges.
			if positions, err := e.store.ListPositions(); err == nil {
				metrics.OpenPositions.Set(float64(len(positions)))
			}
			metrics.BrainHeartbeatAge.Set(e.deadman.SecondsSinceHeartbeat())
			if e.deadman.IsTriggered() {
				metrics.DeadmanTriggered.Set(1)
			} else {
				metrics.DeadmanTriggered.Set(0)
			}
		}
	}
}

// handleKillSignal is called on alerts.kill. Per Wiki security model, this is
// the kill-switch-agent telling us to halt all trading immediately.
//
// Sequence:
//  1. Mark engine as killed — all subsequent commands ACK'd and dropped.
//  2. Force-trigger the dead-man switch → emergency close all positions.
//  3. Publish alerts.critical with kill_received.
//
// Idempotent: repeated kills are no-ops after the first.
func (e *Engine) handleKillSignal() error {
	if !e.killed.CompareAndSwap(false, true) {
		e.logger.Warn("engine: kill signal received again (already killed)")
		return nil
	}
	metrics.KillReceived.Set(1)
	e.logger.Error("engine: KILL SIGNAL RECEIVED — halting trading")

	e.bus.PublishAlert(&domain.AlertEvent{
		AlertType: "kill_received",
		Source:    "execution-engine",
		Message:   "Kill signal received; blocking new commands and closing positions.",
		Severity:  "critical",
		Timestamp: time.Now().UTC(),
	})

	ctx := e.rootCtx
	if ctx == nil {
		ctx = context.Background()
	}
	e.deadman.ForceTrigger(ctx, "kill_received")
	return nil
}

// IsKilled reports whether the kill signal has been received.
func (e *Engine) IsKilled() bool {
	return e.killed.Load()
}

// handleCommand is the main entrypoint for commands.*
func (e *Engine) handleCommand(cmd *domain.Command, ackFunc func() error) error {
	now := time.Now().UTC()
	log := e.logger.With("cmd_id", cmd.CommandID, "type", cmd.CommandType)

	log.Info("engine: received command")
	metrics.CommandsReceived.WithLabelValues(cmd.CommandType).Inc()

	// 0. Kill-signal short-circuit
	if e.killed.Load() {
		log.Warn("engine: rejecting command, engine is killed")
		metrics.CommandsRejected.WithLabelValues("killed", "engine_killed").Inc()
		e.bus.PublishEvent(domain.EventOrderRejected, domain.OrderEvent{
			EventType:    domain.EventOrderRejected,
			CommandID:    cmd.CommandID,
			Status:       "rejected",
			RejectReason: "engine_killed",
			Timestamp:    now,
		})
		return ackFunc()
	}

	// 1. Dead-Man Switch Check
	if e.deadman.IsTriggered() {
		log.Warn("engine: rejecting command, deadman switch is active")
		metrics.CommandsRejected.WithLabelValues("deadman", "deadman_active").Inc()
		e.bus.PublishEvent(domain.EventOrderRejected, domain.OrderEvent{
			EventType:    domain.EventOrderRejected,
			CommandID:    cmd.CommandID,
			Status:       "rejected",
			RejectReason: "deadman_active",
			Timestamp:    now,
		})
		return ackFunc()
	}

	// 2. Schema Validation
	if err := cmd.ValidateSchema(); err != nil {
		log.Warn("engine: invalid schema", "error", err)
		metrics.CommandsRejected.WithLabelValues("schema", "schema_mismatch").Inc()
		return ackFunc() // Drop malformed commands
	}

	// 3. TTL Validation
	if err := cmd.ValidateTTL(now); err != nil {
		log.Warn("engine: command expired", "error", err)
		metrics.CommandsRejected.WithLabelValues("ttl", "command_expired").Inc()
		return ackFunc()
	}

	// 4. HMAC Validation
	if err := cmd.ValidateHMAC(e.hmacSecret); err != nil {
		log.Error("engine: invalid HMAC signature, CRITICAL ALERT", "error", err)
		metrics.CommandsRejected.WithLabelValues("hmac", "invalid_hmac").Inc()
		e.bus.PublishAlert(&domain.AlertEvent{
			AlertType: "invalid_hmac",
			Source:    "execution-engine",
			Message:   fmt.Sprintf("Invalid HMAC for command %s", cmd.CommandID),
			Severity:  "critical",
			Timestamp: time.Now().UTC(),
		})
		return ackFunc() // Drop malicious/corrupted commands
	}

	// 5. Idempotency Check
	seen, err := e.store.HasIdempotencyKey(cmd.IdempotencyKey)
	if err != nil {
		return fmt.Errorf("check idempotency: %w", err) // Return err to NAK and retry
	}
	if seen {
		log.Info("engine: command already processed (idempotency dedup)", "key", cmd.IdempotencyKey)
		metrics.CommandsRejected.WithLabelValues("idempotency", "duplicate").Inc()
		return ackFunc() // ACK and skip
	}

	// 6. Route to specific handler
	var processErr error
	switch cmd.CommandType {
	case domain.CmdPlaceOrder:
		processErr = e.handlePlaceOrder(cmd, log)
	case domain.CmdCancelOrder:
		processErr = e.handleCancelOrder(cmd, log)
	case domain.CmdModifyOrder:
		processErr = e.handleModifyOrder(cmd, log)
	case domain.CmdClosePosition:
		processErr = e.handleClosePosition(cmd, log)
	default:
		log.Warn("engine: unknown command type")
		return ackFunc()
	}

	if processErr != nil {
		log.Error("engine: failed to process command", "error", processErr)
		return processErr // NAK and retry
	}

	// 7. Store Idempotency Key (on success)
	if err := e.store.StoreIdempotencyKey(cmd.IdempotencyKey); err != nil {
		log.Error("engine: failed to store idempotency key", "error", err)
		// We still ACK because the order was placed. We don't want to place it again.
	}

	log.Info("engine: command processed successfully")
	return ackFunc()
}

func (e *Engine) handlePlaceOrder(cmd *domain.Command, log *slog.Logger) error {
	started := time.Now()
	defer func() {
		metrics.OrderPlaceLatency.Observe(time.Since(started).Seconds())
	}()

	payload, err := cmd.ParsePlaceOrderPayload()
	if err != nil {
		log.Error("engine: parse place_order payload failed", "error", err)
		metrics.CommandsRejected.WithLabelValues("malformed", "place_order").Inc()
		e.publishMalformedRejection(cmd, "place_order", err)
		return nil // ACK — payload is broken, retries won't help
	}

	// Fetch balance for risk checks
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	balance, err := e.proxyClient.QueryBalance(ctx)
	if err != nil {
		return fmt.Errorf("query balance: %w", err) // NAK to retry
	}

	// Risk Gate #2
	if err := e.gate.EvaluatePlaceOrder(cmd, payload, balance.AvailableBalanceUSDT); err != nil {
		log.Warn("engine: Gate #2 REJECTED", "error", err)
		rule := "unknown"
		if rej, ok := err.(*risk.RejectionError); ok {
			rule = rej.Rule
		}
		metrics.Gate2Rejections.WithLabelValues(rule).Inc()
		metrics.CommandsRejected.WithLabelValues("gate2", rule).Inc()

		e.bus.PublishEvent(domain.EventOrderRejected, domain.OrderEvent{
			EventType:     domain.EventOrderRejected,
			CommandID:     cmd.CommandID,
			ClientOrderID: payload.ClientOrderID,
			Symbol:        payload.Symbol,
			Side:          payload.Side,
			OrderType:     payload.OrderType,
			Qty:           payload.Qty,
			Price:         payload.Price,
			Status:        "rejected",
			RejectReason:  err.Error(),
			Timestamp:     time.Now().UTC(),
		})

		// Severity by rule:
		//   critical  — config-rule disagreement with Gate #1 (whitelist, confidence,
		//               max_position_size, leverage, slippage, stop_loss_type,
		//               correlated_positions, stop_loss_price). These SHOULD have
		//               been caught by Gate #1; if not, the configs have drifted.
		//   warning   — state-dependent rules (cooldown, drawdown, max_positions).
		//               Legitimately can differ between nodes.
		severity := "critical"
		alertType := "gate2_disagreement"
		if rej, ok := err.(*risk.RejectionError); ok {
			switch rej.Rule {
			case "ticker_cooldown", "daily_drawdown", "max_open_positions", "position_size":
				severity = "warning"
				alertType = "gate2_rejection"
			}
		}
		e.bus.PublishAlert(&domain.AlertEvent{
			AlertType: alertType,
			Source:    "execution-engine",
			Message:   fmt.Sprintf("Gate 2 rejected order %s: %s", cmd.CommandID, err.Error()),
			Severity:  severity,
			Timestamp: time.Now().UTC(),
		})
		return nil // ACK — valid rejection
	}

	// Map to Proxy Request
	req := &proxy.PlaceOrderRequest{
		Symbol:               payload.Symbol,
		Side:                 payload.Side,
		OrderType:            payload.OrderType,
		Qty:                  payload.Qty,
		Price:                payload.Price,
		TimeInForce:          payload.TimeInForce,
		ReduceOnly:           payload.ReduceOnly,
		ClientOrderID:        payload.ClientOrderID,
		StopLossTriggerPrice: payload.RiskContext.StopLoss.TriggerPrice,
		StopLossOrderType:    payload.RiskContext.StopLoss.OrderType,
	}

	for _, tp := range payload.RiskContext.TakeProfit {
		req.TakeProfitLevels = append(req.TakeProfitLevels, proxy.TPLevelRequest{
			Price:  tp.Price,
			QtyPct: tp.QtyPct,
		})
	}

	if ts := payload.RiskContext.TrailingStop; ts != nil {
		req.TrailingStopActivationPrice = ts.ActivationPrice
		req.TrailingStopCallbackRate = ts.CallbackRate
		req.TrailingStopOrderType = ts.OrderType
	}

	// Execute via Proxy
	resp, err := e.proxyClient.PlaceOrder(ctx, req)
	if err != nil {
		return fmt.Errorf("place order proxy call: %w", err) // NAK to retry
	}

	log.Info("engine: order placed", "order_id", resp.OrderID, "status", resp.Status)
	metrics.OrdersPlaced.WithLabelValues(payload.Symbol, payload.Side).Inc()
	now2 := time.Now().UTC()

	// Always emit the submitted event first to keep the lifecycle trace complete.
	e.bus.PublishEvent(domain.EventOrderSubmitted, domain.OrderEvent{
		EventType:     domain.EventOrderSubmitted,
		CommandID:     cmd.CommandID,
		OrderID:       resp.OrderID,
		ClientOrderID: payload.ClientOrderID,
		Symbol:        payload.Symbol,
		Side:          payload.Side,
		OrderType:     payload.OrderType,
		Qty:           payload.Qty,
		Price:         payload.Price,
		Status:        "submitted",
		Timestamp:     now2,
	})

	// Persist the order so we can reconcile it on restart.
	e.store.SaveOrder(&domain.TrackedOrder{
		OrderID:       resp.OrderID,
		ClientOrderID: payload.ClientOrderID,
		Symbol:        payload.Symbol,
		Side:          payload.Side,
		OrderType:     payload.OrderType,
		Qty:           payload.Qty,
		Price:         payload.Price,
		Status:        resp.Status,
		CommandID:     cmd.CommandID,
		SubmittedAt:   now2,
	})

	// Lifecycle transitions driven by proxy response status.
	// Source of truth for async fills is events.order.* from bybit-proxy WS
	// (Phase 4+), but synchronous responses must still populate state correctly.
	switch resp.Status {
	case "filled":
		e.bus.PublishEvent(domain.EventOrderFilled, domain.OrderEvent{
			EventType:     domain.EventOrderFilled,
			CommandID:     cmd.CommandID,
			OrderID:       resp.OrderID,
			ClientOrderID: payload.ClientOrderID,
			Symbol:        payload.Symbol,
			Side:          payload.Side,
			OrderType:     payload.OrderType,
			Qty:           payload.Qty,
			FilledQty:     resp.FilledQty,
			AvgPrice:      resp.AvgPrice,
			Status:        "filled",
			Timestamp:     now2,
		})
		e.openPosition(payload, resp, cmd.CommandID, now2, log)

	case "partial":
		e.bus.PublishEvent(domain.EventOrderPartial, domain.OrderEvent{
			EventType:     domain.EventOrderPartial,
			CommandID:     cmd.CommandID,
			OrderID:       resp.OrderID,
			ClientOrderID: payload.ClientOrderID,
			Symbol:        payload.Symbol,
			Side:          payload.Side,
			OrderType:     payload.OrderType,
			Qty:           payload.Qty,
			FilledQty:     resp.FilledQty,
			AvgPrice:      resp.AvgPrice,
			Status:        "partial",
			Timestamp:     now2,
		})
		// Partial → treat as an open position sized by filled_qty for tracking.
		e.openPosition(payload, resp, cmd.CommandID, now2, log)
	}

	return nil
}

// openPosition persists a TrackedPosition and emits events.position.opened.
// Called when the proxy reports the order filled (fully or partially).
func (e *Engine) openPosition(
	payload *domain.PlaceOrderPayload,
	resp *proxy.PlaceOrderResponse,
	commandID string,
	now time.Time,
	log *slog.Logger,
) {
	size := resp.FilledQty
	if size == "" {
		size = payload.Qty
	}
	entryPrice := resp.AvgPrice
	if entryPrice == "" {
		entryPrice = payload.Price
	}
	pos := &domain.TrackedPosition{
		Symbol:     payload.Symbol,
		Side:       payload.Side,
		Size:       size,
		EntryPrice: entryPrice,
		StopLossID: resp.StopLossOrderID,
		TPOrderIDs: resp.TakeProfitIDs,
		OpenedAt:   now,
		CommandID:  commandID,
	}
	if err := e.store.SavePosition(pos); err != nil {
		log.Error("engine: failed to save position", "error", err, "symbol", payload.Symbol)
	}
	e.bus.PublishEvent(domain.EventPositionOpened, domain.PositionEvent{
		EventType:  domain.EventPositionOpened,
		Symbol:     pos.Symbol,
		Side:       pos.Side,
		Size:       pos.Size,
		EntryPrice: pos.EntryPrice,
		Timestamp:  now,
	})
}

func (e *Engine) handleCancelOrder(cmd *domain.Command, log *slog.Logger) error {
	payload, err := cmd.ParseCancelOrderPayload()
	if err != nil {
		log.Error("engine: parse cancel_order payload failed", "error", err)
		e.publishMalformedRejection(cmd, "cancel_order", err)
		return nil
	}

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	resp, err := e.proxyClient.CancelOrder(ctx, &proxy.CancelOrderRequest{
		Symbol:        payload.Symbol,
		OrderID:       payload.OrderID,
		ClientOrderID: payload.ClientOrderID,
	})
	if err != nil {
		return fmt.Errorf("cancel order proxy call: %w", err) // NAK
	}

	log.Info("engine: order cancelled", "order_id", resp.OrderID)
	return nil
}

func (e *Engine) handleModifyOrder(cmd *domain.Command, log *slog.Logger) error {
	payload, err := cmd.ParseModifyOrderPayload()
	if err != nil {
		log.Error("engine: parse modify_order payload failed", "error", err)
		e.publishMalformedRejection(cmd, "modify_order", err)
		return nil
	}

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	resp, err := e.proxyClient.ModifyOrder(ctx, &proxy.ModifyOrderRequest{
		Symbol:        payload.Symbol,
		OrderID:       payload.OrderID,
		ClientOrderID: payload.ClientOrderID,
		NewQty:        payload.NewQty,
		NewPrice:      payload.NewPrice,
	})
	if err != nil {
		return fmt.Errorf("modify order proxy call: %w", err) // NAK
	}

	log.Info("engine: order modified", "order_id", resp.OrderID)
	return nil
}

func (e *Engine) handleClosePosition(cmd *domain.Command, log *slog.Logger) error {
	payload, err := cmd.ParseClosePositionPayload()
	if err != nil {
		log.Error("engine: parse close_position payload failed", "error", err)
		e.publishMalformedRejection(cmd, "close_position", err)
		return nil
	}

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	resp, err := e.proxyClient.ClosePosition(ctx, &proxy.ClosePositionRequest{
		Symbol: payload.Symbol,
		Qty:    payload.Qty,
	})
	if err != nil {
		return fmt.Errorf("close position proxy call: %w", err) // NAK
	}

	log.Info("engine: position closed", "order_id", resp.OrderID)
	
	// Remove from tracked positions
	e.store.DeletePosition(payload.Symbol)
	e.gate.RecordClosedPosition(payload.Symbol, resp.RealizedPnLUSDT)

	// Publish position.closed event
	e.bus.PublishEvent(domain.EventPositionClosed, domain.PositionEvent{
		EventType: domain.EventPositionClosed,
		Symbol:    payload.Symbol,
		Size:      payload.Qty,
		ExitPrice: resp.ExitPrice,
		PnL:       fmt.Sprintf("%.8f", resp.RealizedPnLUSDT),
		Timestamp: time.Now().UTC(),
	})

	return nil
}

// publishMalformedRejection emits events.order.rejected and a warning alert
// when a command's payload cannot be parsed. Per Wiki audit-trail requirement:
// every rejected command must leave a trace in events.* (not silent drop).
func (e *Engine) publishMalformedRejection(cmd *domain.Command, cmdType string, parseErr error) {
	reason := fmt.Sprintf("malformed_payload:%s: %v", cmdType, parseErr)
	e.bus.PublishEvent(domain.EventOrderRejected, domain.OrderEvent{
		EventType:    domain.EventOrderRejected,
		CommandID:    cmd.CommandID,
		Status:       "rejected",
		RejectReason: reason,
		Timestamp:    time.Now().UTC(),
	})
	e.bus.PublishAlert(&domain.AlertEvent{
		AlertType: "malformed_command",
		Source:    "execution-engine",
		Message:   fmt.Sprintf("Malformed %s command %s: %v", cmdType, cmd.CommandID, parseErr),
		Severity:  "warning",
		Timestamp: time.Now().UTC(),
	})
}
