// Package domain defines the core data structures for the execution engine.
// These structures mirror the Command Contract specification from the Wiki.
package domain

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

// ---------------------------------------------------------------------------
// Command types — matches commands.* NATS subjects
// ---------------------------------------------------------------------------

const (
	CmdPlaceOrder    = "place_order"
	CmdCancelOrder   = "cancel_order"
	CmdModifyOrder   = "modify_order"
	CmdClosePosition = "close_position"
)

// Command is the top-level envelope for every message on commands.*.
// See Wiki: concepts/command-contract.md — Five Mandatory Properties.
type Command struct {
	CommandID     string    `json:"command_id"`
	SchemaVersion string    `json:"schema_version"`
	IssuedAt      time.Time `json:"issued_at"`
	TTLMS         int64     `json:"ttl_ms"`
	IssuedBy      string    `json:"issued_by"`
	Signature     string    `json:"signature"` // "hmac-sha256:<hex>"

	CommandType    string `json:"command_type"`
	IdempotencyKey string `json:"idempotency_key"`

	Payload json.RawMessage `json:"payload"`
}

// PlaceOrderPayload is the payload for command_type = "place_order".
type PlaceOrderPayload struct {
	Symbol        string `json:"symbol"`
	Side          string `json:"side"`     // "Buy" | "Sell"
	OrderType     string `json:"order_type"` // "Limit" | "Market"
	Qty           string `json:"qty"`
	Price         string `json:"price,omitempty"`
	TimeInForce   string `json:"time_in_force"` // "PostOnly" | "GTC" | "IOC"
	ReduceOnly    bool   `json:"reduce_only"`
	ClientOrderID string `json:"client_order_id"`

	RiskContext      RiskContext      `json:"risk_context"`
	DecisionContext  DecisionContext  `json:"decision_context"`
}

// CancelOrderPayload is the payload for command_type = "cancel_order".
type CancelOrderPayload struct {
	Symbol        string `json:"symbol"`
	OrderID       string `json:"order_id,omitempty"`
	ClientOrderID string `json:"client_order_id,omitempty"`
}

// ModifyOrderPayload is the payload for command_type = "modify_order".
type ModifyOrderPayload struct {
	Symbol        string `json:"symbol"`
	OrderID       string `json:"order_id,omitempty"`
	ClientOrderID string `json:"client_order_id,omitempty"`
	NewQty        string `json:"new_qty,omitempty"`
	NewPrice      string `json:"new_price,omitempty"`
}

// ClosePositionPayload is the payload for command_type = "close_position".
type ClosePositionPayload struct {
	Symbol string `json:"symbol"`
	Qty    string `json:"qty,omitempty"` // empty = close all
}

// RiskContext is embedded in every place_order command.
type RiskContext struct {
	MaxPositionValueUSDT float64       `json:"max_position_value_usdt"`
	StopLoss             StopLoss      `json:"stop_loss"`
	TakeProfit           []TPLevel     `json:"take_profit"`
	TrailingStop         *TrailingStop `json:"trailing_stop,omitempty"`
	MaxSlippageBPS       int           `json:"max_slippage_bps"`
	CancelIfNotFilledSec int           `json:"cancel_if_not_filled_seconds"`
}

// TrailingStop — server-side trailing stop on Bybit.
// Algorithm selection (ATR / Chandelier) is performed by the orchestrator;
// the execution-engine just forwards the parameters to bybit-proxy.
// One of ActivationPrice or CallbackRate must be set.
type TrailingStop struct {
	ActivationPrice string `json:"activation_price,omitempty"` // price that arms the trailing logic
	CallbackRate    string `json:"callback_rate,omitempty"`    // % distance from best price
	OrderType       string `json:"order_type,omitempty"`       // "Market" (default)
}

// StopLoss — always server-side on Bybit (survives engine restarts).
type StopLoss struct {
	Type         string `json:"type"`          // always "server"
	TriggerPrice string `json:"trigger_price"`
	OrderType    string `json:"order_type"`    // "Market"
}

// TPLevel is one tier of a take-profit ladder.
type TPLevel struct {
	Price  string `json:"price"`
	QtyPct int    `json:"qty_pct"`
}

// DecisionContext links the order back to the LLM reasoning.
type DecisionContext struct {
	DecisionID       string   `json:"decision_id"`
	SignalSource     string   `json:"signal_source"`
	LLMConfidence    float64  `json:"llm_confidence"`
	SimilarTradesRef []string `json:"similar_trades_ref"`
}

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

// SupportedSchemaVersions lists schema versions we accept.
var SupportedSchemaVersions = map[string]bool{
	"1.0.0": true,
}

// ValidateSchema checks the schema_version field.
func (c *Command) ValidateSchema() error {
	if !SupportedSchemaVersions[c.SchemaVersion] {
		return fmt.Errorf("schema_mismatch: unsupported version %q", c.SchemaVersion)
	}
	return nil
}

// ValidateTTL checks whether the command has expired.
func (c *Command) ValidateTTL(now time.Time) error {
	deadline := c.IssuedAt.Add(time.Duration(c.TTLMS) * time.Millisecond)
	if now.After(deadline) {
		return fmt.Errorf("command_expired: issued_at=%s ttl=%dms now=%s",
			c.IssuedAt.Format(time.RFC3339Nano), c.TTLMS, now.Format(time.RFC3339Nano))
	}
	return nil
}

// ValidateHMAC verifies the HMAC-SHA256 signature.
// The signature field format is "hmac-sha256:<hex>".
// The MAC is computed over the canonical JSON of the command with the
// "signature" field omitted.
func (c *Command) ValidateHMAC(secret []byte) error {
	parts := strings.SplitN(c.Signature, ":", 2)
	if len(parts) != 2 || parts[0] != "hmac-sha256" {
		return fmt.Errorf("invalid signature format: %q", c.Signature)
	}
	expectedHex := parts[1]

	// Build canonical payload (same struct without signature).
	type canonicalCommand struct {
		CommandID      string          `json:"command_id"`
		SchemaVersion  string          `json:"schema_version"`
		IssuedAt       time.Time       `json:"issued_at"`
		TTLMS          int64           `json:"ttl_ms"`
		IssuedBy       string          `json:"issued_by"`
		CommandType    string          `json:"command_type"`
		IdempotencyKey string          `json:"idempotency_key"`
		Payload        json.RawMessage `json:"payload"`
	}
	canonical := canonicalCommand{
		CommandID:      c.CommandID,
		SchemaVersion:  c.SchemaVersion,
		IssuedAt:       c.IssuedAt,
		TTLMS:          c.TTLMS,
		IssuedBy:       c.IssuedBy,
		CommandType:    c.CommandType,
		IdempotencyKey: c.IdempotencyKey,
		Payload:        c.Payload,
	}

	data, err := json.Marshal(canonical)
	if err != nil {
		return fmt.Errorf("failed to marshal canonical command: %w", err)
	}

	mac := hmac.New(sha256.New, secret)
	mac.Write(data)
	computedHex := hex.EncodeToString(mac.Sum(nil))

	if !hmac.Equal([]byte(computedHex), []byte(expectedHex)) {
		return fmt.Errorf("hmac_invalid: signature mismatch")
	}
	return nil
}

// ParsePlaceOrderPayload deserializes the payload as a PlaceOrderPayload.
func (c *Command) ParsePlaceOrderPayload() (*PlaceOrderPayload, error) {
	var p PlaceOrderPayload
	if err := json.Unmarshal(c.Payload, &p); err != nil {
		return nil, fmt.Errorf("failed to parse place_order payload: %w", err)
	}
	return &p, nil
}

// ParseCancelOrderPayload deserializes the payload as a CancelOrderPayload.
func (c *Command) ParseCancelOrderPayload() (*CancelOrderPayload, error) {
	var p CancelOrderPayload
	if err := json.Unmarshal(c.Payload, &p); err != nil {
		return nil, fmt.Errorf("failed to parse cancel_order payload: %w", err)
	}
	return &p, nil
}

// ParseModifyOrderPayload deserializes the payload as a ModifyOrderPayload.
func (c *Command) ParseModifyOrderPayload() (*ModifyOrderPayload, error) {
	var p ModifyOrderPayload
	if err := json.Unmarshal(c.Payload, &p); err != nil {
		return nil, fmt.Errorf("failed to parse modify_order payload: %w", err)
	}
	return &p, nil
}

// ParseClosePositionPayload deserializes the payload as a ClosePositionPayload.
func (c *Command) ParseClosePositionPayload() (*ClosePositionPayload, error) {
	var p ClosePositionPayload
	if err := json.Unmarshal(c.Payload, &p); err != nil {
		return nil, fmt.Errorf("failed to parse close_position payload: %w", err)
	}
	return &p, nil
}
