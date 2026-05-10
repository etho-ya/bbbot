package domain

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"testing"
	"time"
)

// signCommand produces the hmac-sha256:<hex> signature over the canonical form
// (command with no signature field). It mirrors the verification path so we
// can exercise ValidateHMAC positively.
func signCommand(t *testing.T, cmd *Command, secret []byte) {
	t.Helper()
	type canonical struct {
		CommandID      string          `json:"command_id"`
		SchemaVersion  string          `json:"schema_version"`
		IssuedAt       time.Time       `json:"issued_at"`
		TTLMS          int64           `json:"ttl_ms"`
		IssuedBy       string          `json:"issued_by"`
		CommandType    string          `json:"command_type"`
		IdempotencyKey string          `json:"idempotency_key"`
		Payload        json.RawMessage `json:"payload"`
	}
	c := canonical{
		CommandID:      cmd.CommandID,
		SchemaVersion:  cmd.SchemaVersion,
		IssuedAt:       cmd.IssuedAt,
		TTLMS:          cmd.TTLMS,
		IssuedBy:       cmd.IssuedBy,
		CommandType:    cmd.CommandType,
		IdempotencyKey: cmd.IdempotencyKey,
		Payload:        cmd.Payload,
	}
	data, err := json.Marshal(c)
	if err != nil {
		t.Fatalf("marshal canonical: %v", err)
	}
	mac := hmac.New(sha256.New, secret)
	mac.Write(data)
	cmd.Signature = "hmac-sha256:" + hex.EncodeToString(mac.Sum(nil))
}

func freshCommand() *Command {
	return &Command{
		CommandID:      "cmd_test_001",
		SchemaVersion:  "1.0.0",
		IssuedAt:       time.Now().UTC(),
		TTLMS:          30000,
		IssuedBy:       "test",
		CommandType:    CmdPlaceOrder,
		IdempotencyKey: "idem_test_001",
		Payload:        json.RawMessage(`{"symbol":"BTCUSDT"}`),
	}
}

func TestValidateSchema(t *testing.T) {
	cmd := freshCommand()
	if err := cmd.ValidateSchema(); err != nil {
		t.Errorf("expected 1.0.0 to validate, got %v", err)
	}
	cmd.SchemaVersion = "9.9.9"
	if err := cmd.ValidateSchema(); err == nil {
		t.Errorf("expected unknown version to fail")
	}
}

func TestValidateTTL(t *testing.T) {
	cmd := freshCommand()
	if err := cmd.ValidateTTL(cmd.IssuedAt.Add(10 * time.Second)); err != nil {
		t.Errorf("within TTL should pass, got %v", err)
	}
	if err := cmd.ValidateTTL(cmd.IssuedAt.Add(60 * time.Second)); err == nil {
		t.Errorf("past TTL should fail")
	}
}

func TestValidateHMAC_Happy(t *testing.T) {
	secret := []byte("01234567890123456789012345678901") // 32 bytes
	cmd := freshCommand()
	signCommand(t, cmd, secret)
	if err := cmd.ValidateHMAC(secret); err != nil {
		t.Errorf("valid signature rejected: %v", err)
	}
}

func TestValidateHMAC_BadSecret(t *testing.T) {
	secret := []byte("01234567890123456789012345678901")
	cmd := freshCommand()
	signCommand(t, cmd, secret)
	if err := cmd.ValidateHMAC([]byte("wrong-secret-wrong-secret-wrong!")); err == nil {
		t.Errorf("tampered secret should fail")
	}
}

func TestValidateHMAC_Tampered(t *testing.T) {
	secret := []byte("01234567890123456789012345678901")
	cmd := freshCommand()
	signCommand(t, cmd, secret)
	cmd.Payload = json.RawMessage(`{"symbol":"ETHUSDT"}`) // payload swap after signing
	if err := cmd.ValidateHMAC(secret); err == nil {
		t.Errorf("tampered payload should fail")
	}
}

func TestValidateHMAC_MalformedField(t *testing.T) {
	cmd := freshCommand()
	cmd.Signature = "not-a-valid-format"
	if err := cmd.ValidateHMAC([]byte("secret-secret-secret-secret-sec!")); err == nil {
		t.Errorf("malformed signature field should fail")
	}
}

func TestParsePlaceOrderPayload(t *testing.T) {
	cmd := freshCommand()
	cmd.Payload = json.RawMessage(`{
		"symbol":"BTCUSDT","side":"Buy","order_type":"Limit","qty":"0.01","price":"50000",
		"time_in_force":"PostOnly","client_order_id":"coid1",
		"risk_context":{"max_position_value_usdt":100,"stop_loss":{"type":"server","trigger_price":"49000","order_type":"Market"},"take_profit":[{"price":"51000","qty_pct":100}],"max_slippage_bps":10,"cancel_if_not_filled_seconds":60},
		"decision_context":{"decision_id":"d1","signal_source":"test","llm_confidence":0.8}
	}`)
	p, err := cmd.ParsePlaceOrderPayload()
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if p.Symbol != "BTCUSDT" || p.Side != "Buy" {
		t.Errorf("unexpected payload: %+v", p)
	}
	if p.RiskContext.StopLoss.Type != "server" {
		t.Errorf("stop loss not parsed")
	}
}
