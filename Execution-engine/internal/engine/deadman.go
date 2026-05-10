// Package engine contains the dead-man switch logic.
// See Wiki: entities/execution-engine.md — Dead-Man Switch section.
//
// If elapsed time since last heartbeat.brain > N minutes AND positions are open:
//  1. Stop accepting new commands.
//  2. Place aggressive market stops on all positions.
//  3. Publish alerts.critical with brain_heartbeat_lost.
//  4. Await manual intervention or heartbeat recovery.
package engine

import (
	"context"
	"fmt"
	"log/slog"
	"sync"
	"sync/atomic"
	"time"

	"github.com/Crypto-Baron/execution-engine/internal/domain"
	"github.com/Crypto-Baron/execution-engine/internal/proxy"
	"github.com/Crypto-Baron/execution-engine/internal/state"
	"github.com/Crypto-Baron/execution-engine/internal/transport"
)

// DeadManSwitch monitors the brain heartbeat and takes emergency action.
type DeadManSwitch struct {
	timeout       time.Duration
	checkInterval time.Duration
	store         *state.Store
	bus           *transport.Bus
	proxyClient   *proxy.Client
	logger        *slog.Logger

	mu            sync.Mutex
	lastHeartbeat time.Time
	triggered     atomic.Bool
}

// NewDeadManSwitch creates a new dead-man switch.
func NewDeadManSwitch(
	timeoutMinutes int,
	checkIntervalSeconds int,
	store *state.Store,
	bus *transport.Bus,
	proxyClient *proxy.Client,
	logger *slog.Logger,
) *DeadManSwitch {
	return &DeadManSwitch{
		timeout:       time.Duration(timeoutMinutes) * time.Minute,
		checkInterval: time.Duration(checkIntervalSeconds) * time.Second,
		store:         store,
		bus:           bus,
		proxyClient:   proxyClient,
		logger:        logger,
		lastHeartbeat: time.Now().UTC(), // assume brain is alive at startup
	}
}

// RecordHeartbeat is called when a brain heartbeat is received.
func (d *DeadManSwitch) RecordHeartbeat(ts time.Time) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.lastHeartbeat = ts

	// If previously triggered, reset on heartbeat recovery.
	if d.triggered.Load() {
		d.triggered.Store(false)
		d.logger.Info("deadman: brain heartbeat recovered, resuming normal operation")
	}
}

// IsTriggered returns true if the dead-man switch is currently active.
func (d *DeadManSwitch) IsTriggered() bool {
	return d.triggered.Load()
}

// SecondsSinceHeartbeat returns elapsed seconds since the last brain heartbeat.
// Used by the metrics heartbeat loop to populate brain_heartbeat_age_seconds.
func (d *DeadManSwitch) SecondsSinceHeartbeat() float64 {
	d.mu.Lock()
	defer d.mu.Unlock()
	return time.Since(d.lastHeartbeat).Seconds()
}

// ForceTrigger fires the emergency close sequence regardless of heartbeat state.
// Used by kill-signal handler. Safe to call multiple times — only fires once.
func (d *DeadManSwitch) ForceTrigger(ctx context.Context, reason string) {
	if !d.triggered.CompareAndSwap(false, true) {
		return
	}
	d.logger.Error("deadman: FORCE-TRIGGERED", "reason", reason)

	positions, err := d.store.ListPositions()
	if err != nil {
		d.logger.Error("deadman: failed to list positions on force-trigger", "error", err)
		return
	}

	alert := &domain.AlertEvent{
		AlertType: reason,
		Source:    "execution-engine",
		Message: fmt.Sprintf("Force-trigger: %s. %d open positions. Placing emergency stops.",
			reason, len(positions)),
		Severity:  "critical",
		Timestamp: time.Now().UTC(),
	}
	if err := d.bus.PublishAlert(alert); err != nil {
		d.logger.Error("deadman: failed to publish force-trigger alert", "error", err)
	}

	for _, pos := range positions {
		resp, err := d.proxyClient.ClosePosition(ctx, &proxy.ClosePositionRequest{Symbol: pos.Symbol})
		if err != nil {
			d.logger.Error("deadman: force-close failed", "symbol", pos.Symbol, "error", err)
			continue
		}
		d.logger.Warn("deadman: force-close submitted",
			"symbol", pos.Symbol,
			"order_id", resp.OrderID,
		)
	}
}

// Run starts the dead-man switch monitoring loop. Blocks until ctx is cancelled.
func (d *DeadManSwitch) Run(ctx context.Context) {
	ticker := time.NewTicker(d.checkInterval)
	defer ticker.Stop()

	d.logger.Info("deadman: monitoring started",
		"timeout", d.timeout,
		"check_interval", d.checkInterval,
	)

	for {
		select {
		case <-ctx.Done():
			d.logger.Info("deadman: monitoring stopped")
			return
		case <-ticker.C:
			d.check(ctx)
		}
	}
}

func (d *DeadManSwitch) check(ctx context.Context) {
	d.mu.Lock()
	elapsed := time.Since(d.lastHeartbeat)
	d.mu.Unlock()

	if elapsed < d.timeout {
		return // brain is alive
	}

	// Already triggered — don't re-trigger.
	if d.triggered.Load() {
		return
	}

	// Check if any positions are open.
	positions, err := d.store.ListPositions()
	if err != nil {
		d.logger.Error("deadman: failed to list positions", "error", err)
		return
	}

	if len(positions) == 0 {
		d.logger.Warn("deadman: brain heartbeat lost but no open positions, standing by",
			"elapsed", elapsed,
		)
		return
	}

	// TRIGGER: positions open and brain is gone.
	d.triggered.Store(true)
	d.logger.Error("deadman: TRIGGERED — brain heartbeat lost with open positions",
		"elapsed", elapsed,
		"open_positions", len(positions),
	)

	// Step 1: Publish critical alert.
	alert := &domain.AlertEvent{
		AlertType: "brain_heartbeat_lost",
		Source:    "execution-engine",
		Message: fmt.Sprintf(
			"Brain heartbeat lost for %s. %d open positions. Placing emergency stops.",
			elapsed.Round(time.Second), len(positions),
		),
		Severity:  "critical",
		Timestamp: time.Now().UTC(),
	}
	if err := d.bus.PublishAlert(alert); err != nil {
		d.logger.Error("deadman: failed to publish alert", "error", err)
	}

	// Step 2: Place aggressive market close on all positions.
	for _, pos := range positions {
		d.logger.Warn("deadman: closing position",
			"symbol", pos.Symbol,
			"side", pos.Side,
			"size", pos.Size,
		)
		resp, err := d.proxyClient.ClosePosition(ctx, &proxy.ClosePositionRequest{
			Symbol: pos.Symbol,
		})
		if err != nil {
			d.logger.Error("deadman: failed to close position",
				"symbol", pos.Symbol,
				"error", err,
			)
			continue
		}
		d.logger.Warn("deadman: emergency close submitted",
			"symbol", pos.Symbol,
			"order_id", resp.OrderID,
			"status", resp.Status,
		)
	}
}
