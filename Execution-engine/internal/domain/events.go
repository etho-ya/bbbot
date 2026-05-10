package domain

import "time"

// ---------------------------------------------------------------------------
// Event types — published on events.* NATS subjects
// ---------------------------------------------------------------------------

const (
	EventOrderSubmitted  = "order.submitted"
	EventOrderFilled     = "order.filled"
	EventOrderRejected   = "order.rejected"
	EventOrderPartial    = "order.partial"
	EventPositionOpened  = "position.opened"
	EventPositionClosed  = "position.closed"
	EventStopTriggered   = "stop.triggered"
)

// OrderEvent is emitted on events.order.*.
type OrderEvent struct {
	EventType     string    `json:"event_type"`
	CommandID     string    `json:"command_id"`
	OrderID       string    `json:"order_id"`
	ClientOrderID string    `json:"client_order_id"`
	Symbol        string    `json:"symbol"`
	Side          string    `json:"side"`
	OrderType     string    `json:"order_type"`
	Qty           string    `json:"qty"`
	FilledQty     string    `json:"filled_qty,omitempty"`
	Price         string    `json:"price,omitempty"`
	AvgPrice      string    `json:"avg_price,omitempty"`
	Status        string    `json:"status"`   // "submitted","filled","rejected","partial","cancelled"
	RejectReason  string    `json:"reject_reason,omitempty"`
	Timestamp     time.Time `json:"timestamp"`
}

// PositionEvent is emitted on events.position.*.
type PositionEvent struct {
	EventType string    `json:"event_type"`
	Symbol    string    `json:"symbol"`
	Side      string    `json:"side"`
	Size      string    `json:"size"`
	EntryPrice string   `json:"entry_price,omitempty"`
	ExitPrice  string   `json:"exit_price,omitempty"`
	PnL        string   `json:"pnl,omitempty"`
	Timestamp  time.Time `json:"timestamp"`
}

// AlertEvent is published on alerts.critical.
type AlertEvent struct {
	AlertType string    `json:"alert_type"`
	Source    string    `json:"source"` // "execution-engine"
	Message  string    `json:"message"`
	Severity string    `json:"severity"` // "critical", "warning"
	Timestamp time.Time `json:"timestamp"`
}

// ---------------------------------------------------------------------------
// Position tracking (in-memory + BadgerDB)
// ---------------------------------------------------------------------------

// TrackedPosition represents a live position the engine is managing.
type TrackedPosition struct {
	Symbol     string    `json:"symbol"`
	Side       string    `json:"side"`
	Size       string    `json:"size"`
	EntryPrice string    `json:"entry_price"`
	StopLossID string    `json:"stop_loss_order_id,omitempty"`
	TPOrderIDs []string  `json:"tp_order_ids,omitempty"`
	OpenedAt   time.Time `json:"opened_at"`
	CommandID  string    `json:"command_id"` // originating command
}

// TrackedOrder represents an order in flight.
type TrackedOrder struct {
	OrderID       string    `json:"order_id"`
	ClientOrderID string    `json:"client_order_id"`
	Symbol        string    `json:"symbol"`
	Side          string    `json:"side"`
	OrderType     string    `json:"order_type"`
	Qty           string    `json:"qty"`
	Price         string    `json:"price,omitempty"`
	Status        string    `json:"status"`
	CommandID     string    `json:"command_id"`
	SubmittedAt   time.Time `json:"submitted_at"`
}
