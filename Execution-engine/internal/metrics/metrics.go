// Package metrics exposes Prometheus instrumentation for the execution engine.
// See Wiki: entities/observability.md — execution-engine publishes /metrics on
// its Prometheus endpoint. Metric names follow the convention
// execution_engine_<component>_<what>.
package metrics

import (
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
)

var (
	// CommandsReceived — every command that passes basic envelope deserialization,
	// labelled by command_type.
	CommandsReceived = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "execution_engine_commands_received_total",
			Help: "Total commands received on commands.* subjects.",
		},
		[]string{"type"},
	)

	// CommandsRejected — command rejected at a specific validation stage.
	// stage: schema|ttl|hmac|idempotency|deadman|killed|gate2|malformed.
	CommandsRejected = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "execution_engine_commands_rejected_total",
			Help: "Total commands rejected, labelled by validation stage and reason.",
		},
		[]string{"stage", "reason"},
	)

	// OrdersPlaced — successful place_order calls that reached bybit-proxy.
	OrdersPlaced = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "execution_engine_orders_placed_total",
			Help: "Orders successfully submitted to bybit-proxy.",
		},
		[]string{"symbol", "side"},
	)

	// OrderPlaceLatency — wall time from receiving a place_order command to
	// bybit-proxy returning a response. Does NOT include time spent in NATS.
	OrderPlaceLatency = promauto.NewHistogram(
		prometheus.HistogramOpts{
			Name:    "execution_engine_order_place_latency_seconds",
			Help:    "Latency of handlePlaceOrder from start to proxy response.",
			Buckets: prometheus.DefBuckets,
		},
	)

	// Gate2Rejections — Gate #2 rejections broken down by rule.
	Gate2Rejections = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "execution_engine_gate2_rejections_total",
			Help: "Risk Gate #2 rejections, labelled by rule.",
		},
		[]string{"rule"},
	)

	// DeadmanTriggered — 1 if dead-man switch is currently active.
	DeadmanTriggered = promauto.NewGauge(
		prometheus.GaugeOpts{
			Name: "execution_engine_deadman_triggered",
			Help: "1 if the dead-man switch is active, 0 otherwise.",
		},
	)

	// BrainHeartbeatAge — seconds since the last heartbeat.brain was received.
	// Feeds the Alertmanager rule for brain heartbeat staleness.
	BrainHeartbeatAge = promauto.NewGauge(
		prometheus.GaugeOpts{
			Name: "execution_engine_brain_heartbeat_age_seconds",
			Help: "Seconds since the last heartbeat.brain was received.",
		},
	)

	// OpenPositions — number of TrackedPositions currently in BadgerDB.
	OpenPositions = promauto.NewGauge(
		prometheus.GaugeOpts{
			Name: "execution_engine_open_positions",
			Help: "Count of open positions currently tracked locally.",
		},
	)

	// KillReceived — 1 if an alerts.kill has been received this process lifetime.
	KillReceived = promauto.NewGauge(
		prometheus.GaugeOpts{
			Name: "execution_engine_kill_received",
			Help: "1 if alerts.kill has been received, 0 otherwise.",
		},
	)
)
