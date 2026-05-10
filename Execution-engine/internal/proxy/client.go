// Package proxy provides an HTTP client for communicating with the bybit-proxy.
// The bybit-proxy is the only component with outbound Bybit access.
// See Wiki: entities/bybit-proxy.md — execution-engine calls it via internal HTTP.
package proxy

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"time"

	"github.com/Crypto-Baron/execution-engine/internal/config"
)

// Client is the HTTP client for bybit-proxy.
type Client struct {
	baseURL    string
	httpClient *http.Client
	maxRetries int
	retryWait  time.Duration
	logger     *slog.Logger
}

// NewClient creates a new proxy client.
func NewClient(cfg config.ProxyConfig, logger *slog.Logger) *Client {
	return &Client{
		baseURL: cfg.BaseURL,
		httpClient: &http.Client{
			Timeout: cfg.Timeout,
		},
		maxRetries: cfg.MaxRetries,
		retryWait:  time.Duration(cfg.RetryBackoffMs) * time.Millisecond,
		logger:     logger,
	}
}

// ---------------------------------------------------------------------------
// Request / Response types
// ---------------------------------------------------------------------------

// PlaceOrderRequest is sent to bybit-proxy to place an order.
type PlaceOrderRequest struct {
	Symbol        string `json:"symbol"`
	Side          string `json:"side"`
	OrderType     string `json:"order_type"`
	Qty           string `json:"qty"`
	Price         string `json:"price,omitempty"`
	TimeInForce   string `json:"time_in_force"`
	ReduceOnly    bool   `json:"reduce_only"`
	ClientOrderID string `json:"client_order_id"`

	// Stop-loss (server-side on Bybit)
	StopLossTriggerPrice string `json:"stop_loss_trigger_price,omitempty"`
	StopLossOrderType    string `json:"stop_loss_order_type,omitempty"`

	// Take-profit levels
	TakeProfitLevels []TPLevelRequest `json:"take_profit_levels,omitempty"`

	// Trailing stop (server-side on Bybit). Optional.
	TrailingStopActivationPrice string `json:"trailing_stop_activation_price,omitempty"`
	TrailingStopCallbackRate    string `json:"trailing_stop_callback_rate,omitempty"`
	TrailingStopOrderType       string `json:"trailing_stop_order_type,omitempty"`
}

// TPLevelRequest represents one tier of a TP ladder.
type TPLevelRequest struct {
	Price  string `json:"price"`
	QtyPct int    `json:"qty_pct"`
}

// PlaceOrderResponse is returned from bybit-proxy after placing an order.
type PlaceOrderResponse struct {
	OrderID       string `json:"order_id"`
	ClientOrderID string `json:"client_order_id"`
	// Status — one of "submitted" (resting order), "filled", "partial", "rejected".
	Status       string `json:"status"`
	RejectReason string `json:"reject_reason,omitempty"`

	// Fill information (populated if status == "filled" or "partial").
	FilledQty string `json:"filled_qty,omitempty"`
	AvgPrice  string `json:"avg_price,omitempty"`

	// Server-side protective orders actually placed on Bybit.
	StopLossOrderID string   `json:"stop_loss_order_id,omitempty"`
	TakeProfitIDs   []string `json:"take_profit_order_ids,omitempty"`
}

// OpenPosition is returned by ListOpenPositions.
type OpenPosition struct {
	Symbol     string `json:"symbol"`
	Side       string `json:"side"`
	Size       string `json:"size"`
	EntryPrice string `json:"entry_price"`
}

// OpenOrder is returned by ListOpenOrders.
type OpenOrder struct {
	OrderID       string `json:"order_id"`
	ClientOrderID string `json:"client_order_id"`
	Symbol        string `json:"symbol"`
	Side          string `json:"side"`
	OrderType     string `json:"order_type"`
	Qty           string `json:"qty"`
	Price         string `json:"price,omitempty"`
	Status        string `json:"status"`
}

// CancelOrderRequest cancels a pending order.
type CancelOrderRequest struct {
	Symbol        string `json:"symbol"`
	OrderID       string `json:"order_id,omitempty"`
	ClientOrderID string `json:"client_order_id,omitempty"`
}

// CancelOrderResponse is returned from bybit-proxy.
type CancelOrderResponse struct {
	OrderID string `json:"order_id"`
	Status  string `json:"status"`
}

// ModifyOrderRequest modifies a pending order.
type ModifyOrderRequest struct {
	Symbol        string `json:"symbol"`
	OrderID       string `json:"order_id,omitempty"`
	ClientOrderID string `json:"client_order_id,omitempty"`
	NewQty        string `json:"new_qty,omitempty"`
	NewPrice      string `json:"new_price,omitempty"`
}

// ModifyOrderResponse is returned from bybit-proxy.
type ModifyOrderResponse struct {
	OrderID string `json:"order_id"`
	Status  string `json:"status"`
}

// ClosePositionRequest market-closes a position.
type ClosePositionRequest struct {
	Symbol string `json:"symbol"`
	Qty    string `json:"qty,omitempty"` // empty = close full
}

// ClosePositionResponse is returned from bybit-proxy.
type ClosePositionResponse struct {
	OrderID         string  `json:"order_id"`
	Status          string  `json:"status"`
	ExitPrice       string  `json:"exit_price,omitempty"`
	RealizedPnLUSDT float64 `json:"realized_pnl_usdt,omitempty"`
}

// BalanceResponse is returned from proxy.query.balance.
type BalanceResponse struct {
	TotalEquityUSDT    float64 `json:"total_equity_usdt"`
	AvailableBalanceUSDT float64 `json:"available_balance_usdt"`
}

// ---------------------------------------------------------------------------
// API methods
// ---------------------------------------------------------------------------

// PlaceOrder sends a place-order request to bybit-proxy.
func (c *Client) PlaceOrder(ctx context.Context, req *PlaceOrderRequest) (*PlaceOrderResponse, error) {
	var resp PlaceOrderResponse
	err := c.doPost(ctx, "/api/v1/order/place", req, &resp)
	return &resp, err
}

// CancelOrder sends a cancel-order request to bybit-proxy.
func (c *Client) CancelOrder(ctx context.Context, req *CancelOrderRequest) (*CancelOrderResponse, error) {
	var resp CancelOrderResponse
	err := c.doPost(ctx, "/api/v1/order/cancel", req, &resp)
	return &resp, err
}

// ModifyOrder sends a modify-order request to bybit-proxy.
func (c *Client) ModifyOrder(ctx context.Context, req *ModifyOrderRequest) (*ModifyOrderResponse, error) {
	var resp ModifyOrderResponse
	err := c.doPost(ctx, "/api/v1/order/modify", req, &resp)
	return &resp, err
}

// ClosePosition sends a close-position request to bybit-proxy.
func (c *Client) ClosePosition(ctx context.Context, req *ClosePositionRequest) (*ClosePositionResponse, error) {
	var resp ClosePositionResponse
	err := c.doPost(ctx, "/api/v1/position/close", req, &resp)
	return &resp, err
}

// QueryBalance fetches the current account balance from bybit-proxy.
func (c *Client) QueryBalance(ctx context.Context) (*BalanceResponse, error) {
	var resp BalanceResponse
	err := c.doGet(ctx, "/api/v1/account/balance", &resp)
	return &resp, err
}

// ListOpenPositions fetches all currently open positions on Bybit.
// Used for recovery/reconciliation on engine startup.
func (c *Client) ListOpenPositions(ctx context.Context) ([]OpenPosition, error) {
	var resp struct {
		Positions []OpenPosition `json:"positions"`
	}
	err := c.doGet(ctx, "/api/v1/position/list", &resp)
	return resp.Positions, err
}

// ListOpenOrders fetches all resting orders on Bybit.
// Used for recovery/reconciliation on engine startup.
func (c *Client) ListOpenOrders(ctx context.Context) ([]OpenOrder, error) {
	var resp struct {
		Orders []OpenOrder `json:"orders"`
	}
	err := c.doGet(ctx, "/api/v1/order/open", &resp)
	return resp.Orders, err
}

// ---------------------------------------------------------------------------
// HTTP helpers with retry
// ---------------------------------------------------------------------------

func (c *Client) doPost(ctx context.Context, path string, body interface{}, result interface{}) error {
	data, err := json.Marshal(body)
	if err != nil {
		return fmt.Errorf("marshal request: %w", err)
	}

	var lastErr error
	for attempt := 0; attempt <= c.maxRetries; attempt++ {
		if attempt > 0 {
			wait := c.retryWait * time.Duration(1<<(attempt-1)) // exponential backoff
			c.logger.Warn("proxy: retrying request",
				"path", path,
				"attempt", attempt,
				"wait", wait,
			)
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(wait):
			}
		}

		req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+path, bytes.NewReader(data))
		if err != nil {
			return fmt.Errorf("create request: %w", err)
		}
		req.Header.Set("Content-Type", "application/json")

		resp, err := c.httpClient.Do(req)
		if err != nil {
			lastErr = fmt.Errorf("proxy POST %s: %w", path, err)
			continue
		}

		respBody, err := io.ReadAll(resp.Body)
		resp.Body.Close()

		if resp.StatusCode >= 500 {
			lastErr = fmt.Errorf("proxy POST %s: status %d body=%s", path, resp.StatusCode, string(respBody))
			continue // retry on 5xx
		}

		if resp.StatusCode >= 400 {
			return fmt.Errorf("proxy POST %s: status %d body=%s", path, resp.StatusCode, string(respBody))
		}

		if err != nil {
			return fmt.Errorf("read response: %w", err)
		}

		if err := json.Unmarshal(respBody, result); err != nil {
			return fmt.Errorf("unmarshal response: %w", err)
		}
		return nil
	}
	return lastErr
}

func (c *Client) doGet(ctx context.Context, path string, result interface{}) error {
	var lastErr error
	for attempt := 0; attempt <= c.maxRetries; attempt++ {
		if attempt > 0 {
			wait := c.retryWait * time.Duration(1<<(attempt-1))
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(wait):
			}
		}

		req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+path, nil)
		if err != nil {
			return fmt.Errorf("create request: %w", err)
		}

		resp, err := c.httpClient.Do(req)
		if err != nil {
			lastErr = fmt.Errorf("proxy GET %s: %w", path, err)
			continue
		}

		respBody, err := io.ReadAll(resp.Body)
		resp.Body.Close()

		if resp.StatusCode >= 500 {
			lastErr = fmt.Errorf("proxy GET %s: status %d", path, resp.StatusCode)
			continue
		}

		if resp.StatusCode >= 400 {
			return fmt.Errorf("proxy GET %s: status %d body=%s", path, resp.StatusCode, string(respBody))
		}

		if err != nil {
			return fmt.Errorf("read response: %w", err)
		}

		if err := json.Unmarshal(respBody, result); err != nil {
			return fmt.Errorf("unmarshal response: %w", err)
		}
		return nil
	}
	return lastErr
}
