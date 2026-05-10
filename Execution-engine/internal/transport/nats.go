// Package transport handles all NATS JetStream communication.
// See Wiki: concepts/nats-message-bus.md for stream topology and
// security/permission model.
package transport

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"github.com/nats-io/nats.go"

	"github.com/Crypto-Baron/execution-engine/internal/config"
	"github.com/Crypto-Baron/execution-engine/internal/domain"
)

// NATS subjects (per Wiki stream topology).
const (
	SubjCommandOrderPlace    = "commands.order.place"
	SubjCommandOrderCancel   = "commands.order.cancel"
	SubjCommandOrderModify   = "commands.order.modify"
	SubjCommandPositionClose = "commands.position.close"
	SubjAlertKill            = "alerts.kill"

	SubjEventsPrefix         = "events."
	SubjHeartbeatExecution   = "heartbeat.execution"
	SubjHeartbeatBrain       = "heartbeat.brain"
	SubjAlertsCritical       = "alerts.critical"

	// JetStream consumer names
	ConsumerCommands = "execution-engine-commands"
	ConsumerKill     = "execution-engine-kill"
	ConsumerBrainHB  = "execution-engine-brain-hb"
)

// CommandHandler is called for each inbound command.
type CommandHandler func(cmd *domain.Command, ackFunc func() error) error

// KillHandler is called when an alerts.kill message arrives.
type KillHandler func() error

// HeartbeatHandler is called when a brain heartbeat is received.
type HeartbeatHandler func(ts time.Time)

// Bus wraps NATS connection and JetStream context.
type Bus struct {
	nc      *nats.Conn
	js      nats.JetStreamContext
	logger  *slog.Logger
	subs    []*nats.Subscription
	ackWait time.Duration
}

// NewBus creates a NATS connection with optional mTLS and returns a Bus.
func NewBus(cfg config.NATSConfig, logger *slog.Logger) (*Bus, error) {
	opts := []nats.Option{
		nats.Name("execution-engine"),
		nats.MaxReconnects(cfg.MaxReconnects),
		nats.ReconnectWait(cfg.ReconnectWait),
		nats.DisconnectErrHandler(func(_ *nats.Conn, err error) {
			logger.Warn("nats: disconnected", "error", err)
		}),
		nats.ReconnectHandler(func(_ *nats.Conn) {
			logger.Info("nats: reconnected")
		}),
		nats.ClosedHandler(func(_ *nats.Conn) {
			logger.Warn("nats: connection closed")
		}),
	}

	// mTLS if configured
	if cfg.TLSCert != "" && cfg.TLSKey != "" && cfg.TLSCA != "" {
		opts = append(opts, nats.ClientCert(cfg.TLSCert, cfg.TLSKey))
		opts = append(opts, nats.RootCAs(cfg.TLSCA))
	}

	// Auth: prefer nkey seed (ADR-004), fall back to creds file if provided.
	if cfg.NkeySeedFile != "" {
		nkeyOpt, err := nats.NkeyOptionFromSeed(cfg.NkeySeedFile)
		if err != nil {
			return nil, fmt.Errorf("load nkey seed %s: %w", cfg.NkeySeedFile, err)
		}
		opts = append(opts, nkeyOpt)
	} else if cfg.CredsFile != "" {
		opts = append(opts, nats.UserCredentials(cfg.CredsFile))
	}

	nc, err := nats.Connect(cfg.URL, opts...)
	if err != nil {
		return nil, fmt.Errorf("nats connect %s: %w", cfg.URL, err)
	}

	js, err := nc.JetStream()
	if err != nil {
		nc.Close()
		return nil, fmt.Errorf("jetstream init: %w", err)
	}

	ackWait := cfg.AckWait
	if ackWait <= 0 {
		ackWait = 30 * time.Second
	}
	return &Bus{nc: nc, js: js, logger: logger, ackWait: ackWait}, nil
}

// Close drains and closes the NATS connection.
func (b *Bus) Close() error {
	for _, sub := range b.subs {
		if err := sub.Drain(); err != nil {
			b.logger.Warn("nats: drain subscription failed", "error", err)
		}
	}
	b.nc.Close()
	return nil
}

// ---------------------------------------------------------------------------
// Subscribers
// ---------------------------------------------------------------------------

// SubscribeCommands subscribes to all commands.* subjects via JetStream
// with explicit ACK and MaxDeliver: 3 (per Wiki spec).
func (b *Bus) SubscribeCommands(handler CommandHandler) error {
	subjects := []string{
		SubjCommandOrderPlace,
		SubjCommandOrderCancel,
		SubjCommandOrderModify,
		SubjCommandPositionClose,
	}

	for _, subj := range subjects {
		subj := subj // capture
		sub, err := b.js.Subscribe(subj, func(msg *nats.Msg) {
			var cmd domain.Command
			if err := json.Unmarshal(msg.Data, &cmd); err != nil {
				b.logger.Error("nats: failed to unmarshal command",
					"subject", subj,
					"error", err,
				)
				// NAK so it gets redelivered (up to MaxDeliver)
				msg.Nak()
				return
			}

			ackFunc := func() error {
				return msg.Ack()
			}

			if err := handler(&cmd, ackFunc); err != nil {
				b.logger.Error("nats: command handler error",
					"subject", subj,
					"command_id", cmd.CommandID,
					"error", err,
				)
				msg.Nak()
				return
			}
		}, nats.Durable(ConsumerCommands+"-"+sanitizeDurable(subj)),
			nats.AckExplicit(),
			nats.MaxDeliver(3),
			nats.AckWait(b.ackWait),
			nats.DeliverAll(),
			nats.ManualAck(),
		)
		if err != nil {
			return fmt.Errorf("subscribe %s: %w", subj, err)
		}
		b.subs = append(b.subs, sub)
		b.logger.Info("nats: subscribed", "subject", subj)
	}
	return nil
}

// sanitizeDurable replaces characters disallowed in NATS consumer names.
// JetStream durable names cannot contain '.', '>', '*', ' '.
func sanitizeDurable(s string) string {
	r := strings.NewReplacer(".", "_", ">", "_", "*", "_", " ", "_")
	return r.Replace(s)
}

// SubscribeKillSignal subscribes to alerts.kill for emergency shutdown.
// Uses core NATS (non-JetStream): kill is fire-and-forget — no replay semantics wanted
// on restart (a stale kill from last week must not halt a fresh engine).
func (b *Bus) SubscribeKillSignal(handler KillHandler) error {
	sub, err := b.nc.Subscribe(SubjAlertKill, func(msg *nats.Msg) {
		b.logger.Warn("nats: KILL signal received")
		if err := handler(); err != nil {
			b.logger.Error("nats: kill handler error", "error", err)
		}
	})
	if err != nil {
		return fmt.Errorf("subscribe %s: %w", SubjAlertKill, err)
	}
	b.subs = append(b.subs, sub)
	b.logger.Info("nats: subscribed", "subject", SubjAlertKill)
	return nil
}

// SubscribeBrainHeartbeat subscribes to heartbeat.brain for dead-man switch.
func (b *Bus) SubscribeBrainHeartbeat(handler HeartbeatHandler) error {
	sub, err := b.nc.Subscribe(SubjHeartbeatBrain, func(msg *nats.Msg) {
		handler(time.Now().UTC())
	})
	if err != nil {
		return fmt.Errorf("subscribe %s: %w", SubjHeartbeatBrain, err)
	}
	b.subs = append(b.subs, sub)
	b.logger.Info("nats: subscribed", "subject", SubjHeartbeatBrain)
	return nil
}

// ---------------------------------------------------------------------------
// Publishers
// ---------------------------------------------------------------------------

// PublishEvent publishes an event to the appropriate events.* subject.
func (b *Bus) PublishEvent(eventType string, event interface{}) error {
	data, err := json.Marshal(event)
	if err != nil {
		return fmt.Errorf("marshal event: %w", err)
	}
	subj := SubjEventsPrefix + eventType
	_, err = b.js.Publish(subj, data)
	if err != nil {
		return fmt.Errorf("publish %s: %w", subj, err)
	}
	b.logger.Debug("nats: published event", "subject", subj)
	return nil
}

// PublishHeartbeat publishes the execution engine heartbeat.
func (b *Bus) PublishHeartbeat() error {
	hb := map[string]interface{}{
		"source":    "execution-engine",
		"timestamp": time.Now().UTC(),
	}
	data, err := json.Marshal(hb)
	if err != nil {
		return err
	}
	return b.nc.Publish(SubjHeartbeatExecution, data)
}

// PublishAlert publishes a critical alert.
func (b *Bus) PublishAlert(alert *domain.AlertEvent) error {
	data, err := json.Marshal(alert)
	if err != nil {
		return fmt.Errorf("marshal alert: %w", err)
	}
	_, err = b.js.Publish(SubjAlertsCritical, data)
	if err != nil {
		return fmt.Errorf("publish alert: %w", err)
	}
	b.logger.Warn("nats: published critical alert", "type", alert.AlertType)
	return nil
}
