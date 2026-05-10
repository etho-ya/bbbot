// Package risk implements Risk Gate #2, the execution-engine's independent
// pre-order validation layer.
// See Wiki: concepts/risk-gates.md — Gate #2 uses its own local config,
// never fetches rules from the orchestrator at runtime.
package risk

import (
	"fmt"
	"log/slog"
	"strconv"
	"sync"
	"time"

	"github.com/Crypto-Baron/execution-engine/internal/config"
	"github.com/Crypto-Baron/execution-engine/internal/domain"
	"github.com/Crypto-Baron/execution-engine/internal/state"
)

// Gate is the Risk Gate #2 evaluator.
type Gate struct {
	cfg    config.RiskGateConfig
	store  *state.Store
	logger *slog.Logger

	mu              sync.Mutex
	dailyPnL        float64   // tracks realized PnL for current day
	dayStart        time.Time // UTC start of current trading day
	lastCloseTimes  map[string]time.Time // symbol → last position close time
}

// NewGate creates a new Risk Gate #2 instance.
func NewGate(cfg config.RiskGateConfig, store *state.Store, logger *slog.Logger) *Gate {
	now := time.Now().UTC()
	return &Gate{
		cfg:            cfg,
		store:          store,
		logger:         logger,
		dayStart:       time.Date(now.Year(), now.Month(), now.Day(), 0, 0, 0, 0, time.UTC),
		lastCloseTimes: make(map[string]time.Time),
	}
}

// RejectionError carries the structured reason for a Gate #2 rejection.
type RejectionError struct {
	Rule   string
	Reason string
}

func (e *RejectionError) Error() string {
	return fmt.Sprintf("gate2_rejection: rule=%s reason=%s", e.Rule, e.Reason)
}

// EvaluatePlaceOrder runs all Gate #2 rules against a place_order command.
// Returns nil if the order passes; a *RejectionError if it fails.
func (g *Gate) EvaluatePlaceOrder(cmd *domain.Command, payload *domain.PlaceOrderPayload, accountBalanceUSDT float64) error {
	g.mu.Lock()
	defer g.mu.Unlock()

	// Reset daily PnL counter if day has rolled over.
	g.maybeResetDay()

	// Rule: Instrument whitelist
	if err := g.checkWhitelist(payload.Symbol); err != nil {
		return err
	}

	// Rule: LLM confidence threshold
	if err := g.checkConfidence(payload.DecisionContext.LLMConfidence); err != nil {
		return err
	}

	// Rule: Max open positions
	if err := g.checkMaxPositions(); err != nil {
		return err
	}

	// Rule: Position size limit
	if err := g.checkPositionSize(payload, accountBalanceUSDT); err != nil {
		return err
	}

	// Rule: Daily drawdown stop
	if err := g.checkDailyDrawdown(accountBalanceUSDT); err != nil {
		return err
	}

	// Rule: Ticker cooldown
	if err := g.checkTickerCooldown(payload.Symbol); err != nil {
		return err
	}

	// Rule: Max slippage
	if err := g.checkSlippage(payload.RiskContext.MaxSlippageBPS); err != nil {
		return err
	}

	// Rule: Stop-loss must be server-side
	if err := g.checkStopLossType(payload.RiskContext.StopLoss); err != nil {
		return err
	}

	// Rule: Leverage cap
	if err := g.checkLeverage(payload, accountBalanceUSDT); err != nil {
		return err
	}

	// Rule: Correlated positions
	if err := g.checkCorrelation(payload.Symbol); err != nil {
		return err
	}

	g.logger.Info("gate2: all rules passed",
		"symbol", payload.Symbol,
		"command_id", cmd.CommandID,
	)
	return nil
}

// RecordClosedPosition updates internal state when a position is closed.
func (g *Gate) RecordClosedPosition(symbol string, pnl float64) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.lastCloseTimes[symbol] = time.Now().UTC()
	g.dailyPnL += pnl
}

// ---------------------------------------------------------------------------
// Individual rule checks
// ---------------------------------------------------------------------------

func (g *Gate) checkWhitelist(symbol string) error {
	if len(g.cfg.InstrumentWhitelist) == 0 {
		return nil // no whitelist = accept all
	}
	for _, allowed := range g.cfg.InstrumentWhitelist {
		if allowed == symbol {
			return nil
		}
	}
	return &RejectionError{
		Rule:   "instrument_whitelist",
		Reason: fmt.Sprintf("symbol %s not in whitelist", symbol),
	}
}

func (g *Gate) checkConfidence(confidence float64) error {
	if confidence < g.cfg.LLMConfidenceMin {
		return &RejectionError{
			Rule:   "llm_confidence",
			Reason: fmt.Sprintf("confidence %.2f < min %.2f", confidence, g.cfg.LLMConfidenceMin),
		}
	}
	return nil
}

func (g *Gate) checkMaxPositions() error {
	positions, err := g.store.ListPositions()
	if err != nil {
		return fmt.Errorf("failed to list positions: %w", err)
	}
	if len(positions) >= g.cfg.MaxOpenPositions {
		return &RejectionError{
			Rule:   "max_open_positions",
			Reason: fmt.Sprintf("currently %d open (max %d)", len(positions), g.cfg.MaxOpenPositions),
		}
	}
	return nil
}

func (g *Gate) checkPositionSize(payload *domain.PlaceOrderPayload, accountBalance float64) error {
	if accountBalance <= 0 {
		return &RejectionError{
			Rule:   "position_size",
			Reason: "account balance unavailable or zero",
		}
	}
	maxValue := accountBalance * (g.cfg.MaxPositionSizePct / 100.0)
	if payload.RiskContext.MaxPositionValueUSDT > maxValue {
		return &RejectionError{
			Rule:   "position_size",
			Reason: fmt.Sprintf("position value %.2f USDT exceeds max %.2f USDT (%.2f%% of %.2f)",
				payload.RiskContext.MaxPositionValueUSDT, maxValue, g.cfg.MaxPositionSizePct, accountBalance),
		}
	}
	return nil
}

func (g *Gate) checkDailyDrawdown(accountBalance float64) error {
	if accountBalance <= 0 {
		return nil
	}
	maxLoss := accountBalance * (g.cfg.DailyDrawdownStopPct / 100.0)
	if g.dailyPnL < 0 && (-g.dailyPnL) >= maxLoss {
		return &RejectionError{
			Rule:   "daily_drawdown",
			Reason: fmt.Sprintf("daily PnL %.2f exceeds max loss %.2f (%.2f%% of %.2f)",
				g.dailyPnL, -maxLoss, g.cfg.DailyDrawdownStopPct, accountBalance),
		}
	}
	return nil
}

func (g *Gate) checkTickerCooldown(symbol string) error {
	if g.cfg.TickerCooldownMinutes <= 0 {
		return nil
	}
	lastClose, exists := g.lastCloseTimes[symbol]
	if !exists {
		return nil
	}
	cooldown := time.Duration(g.cfg.TickerCooldownMinutes) * time.Minute
	if time.Since(lastClose) < cooldown {
		return &RejectionError{
			Rule:   "ticker_cooldown",
			Reason: fmt.Sprintf("symbol %s closed %s ago, cooldown is %s",
				symbol, time.Since(lastClose).Round(time.Second), cooldown),
		}
	}
	return nil
}

func (g *Gate) checkSlippage(requestedBPS int) error {
	if g.cfg.MaxSlippageBPS > 0 && requestedBPS > g.cfg.MaxSlippageBPS {
		return &RejectionError{
			Rule:   "max_slippage",
			Reason: fmt.Sprintf("requested slippage %d bps > max %d bps", requestedBPS, g.cfg.MaxSlippageBPS),
		}
	}
	return nil
}

func (g *Gate) checkStopLossType(sl domain.StopLoss) error {
	if sl.Type != "server" {
		return &RejectionError{
			Rule:   "stop_loss_type",
			Reason: fmt.Sprintf("stop_loss.type must be 'server', got %q", sl.Type),
		}
	}
	if sl.TriggerPrice == "" {
		return &RejectionError{
			Rule:   "stop_loss_price",
			Reason: "stop_loss.trigger_price is required",
		}
	}
	// Validate that the price is a valid number
	if _, err := strconv.ParseFloat(sl.TriggerPrice, 64); err != nil {
		return &RejectionError{
			Rule:   "stop_loss_price",
			Reason: fmt.Sprintf("stop_loss.trigger_price %q is not a valid number", sl.TriggerPrice),
		}
	}
	return nil
}

// checkLeverage rejects orders whose notional (qty * price) exceeds
// MaxLeverage * accountBalance. qty and price are parsed from the command
// payload. If either is missing/unparseable, we conservatively reject.
func (g *Gate) checkLeverage(payload *domain.PlaceOrderPayload, accountBalance float64) error {
	if g.cfg.MaxLeverage <= 0 || accountBalance <= 0 {
		return nil // rule disabled or balance unavailable (checkPositionSize already handled)
	}
	qty, err := strconv.ParseFloat(payload.Qty, 64)
	if err != nil || qty <= 0 {
		return &RejectionError{Rule: "leverage", Reason: fmt.Sprintf("invalid qty %q", payload.Qty)}
	}
	// For market orders price may be empty — use MaxPositionValueUSDT as best
	// available notional estimate from RiskContext.
	var notional float64
	if payload.Price != "" {
		if p, err := strconv.ParseFloat(payload.Price, 64); err == nil {
			notional = qty * p
		}
	}
	if notional == 0 {
		notional = payload.RiskContext.MaxPositionValueUSDT
	}
	if notional <= 0 {
		return nil // cannot evaluate
	}
	maxNotional := accountBalance * g.cfg.MaxLeverage
	if notional > maxNotional {
		return &RejectionError{
			Rule: "leverage",
			Reason: fmt.Sprintf("notional %.2f USDT exceeds max leverage %.1fx of balance %.2f (cap %.2f)",
				notional, g.cfg.MaxLeverage, accountBalance, maxNotional),
		}
	}
	return nil
}

// checkCorrelation rejects a new position if any currently open position has
// a correlation coefficient with the new symbol above MaxCorrelation.
// If the correlation matrix is empty or MaxCorrelation is <= 0, this rule is a no-op.
func (g *Gate) checkCorrelation(symbol string) error {
	if g.cfg.MaxCorrelation <= 0 || len(g.cfg.CorrelationMatrix) == 0 {
		return nil
	}
	row, ok := g.cfg.CorrelationMatrix[symbol]
	if !ok {
		return nil
	}
	positions, err := g.store.ListPositions()
	if err != nil {
		return fmt.Errorf("correlation check: list positions: %w", err)
	}
	for _, p := range positions {
		corr, ok := row[p.Symbol]
		if !ok {
			continue
		}
		abs := corr
		if abs < 0 {
			abs = -abs
		}
		if abs > g.cfg.MaxCorrelation {
			return &RejectionError{
				Rule: "correlated_positions",
				Reason: fmt.Sprintf("open position %s has correlation %.2f with %s (max %.2f)",
					p.Symbol, corr, symbol, g.cfg.MaxCorrelation),
			}
		}
	}
	return nil
}

func (g *Gate) maybeResetDay() {
	now := time.Now().UTC()
	today := time.Date(now.Year(), now.Month(), now.Day(), 0, 0, 0, 0, time.UTC)
	if today.After(g.dayStart) {
		g.dailyPnL = 0
		g.dayStart = today
		g.logger.Info("gate2: daily PnL counter reset")
	}
}
