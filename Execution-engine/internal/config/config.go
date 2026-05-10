// Package config loads and validates the execution-engine configuration.
package config

import (
	"fmt"
	"os"
	"time"

	"gopkg.in/yaml.v3"
)

// Config is the root configuration for the execution engine.
type Config struct {
	NATS     NATSConfig     `yaml:"nats"`
	Badger   BadgerConfig   `yaml:"badger"`
	Proxy    ProxyConfig    `yaml:"proxy"`
	RiskGate RiskGateConfig `yaml:"risk_gate"`
	DeadMan  DeadManConfig  `yaml:"dead_man"`
	Metrics  MetricsConfig  `yaml:"metrics"`
	HMAC     HMACConfig     `yaml:"hmac"`
}

// NATSConfig holds NATS connection settings.
type NATSConfig struct {
	URL           string        `yaml:"url"`             // e.g. "nats://127.0.0.1:4222"
	NkeySeedFile  string        `yaml:"nkey_seed_file"`  // path to the service nkey seed (preferred per ADR-004)
	CredsFile     string        `yaml:"creds_file"`      // NATS credentials file (alternative to nkey)
	TLSCert       string        `yaml:"tls_cert"`        // mTLS client cert (optional)
	TLSKey        string        `yaml:"tls_key"`         // mTLS client key
	TLSCA         string        `yaml:"tls_ca"`          // CA cert
	MaxReconnects int           `yaml:"max_reconnects"`
	ReconnectWait time.Duration `yaml:"reconnect_wait"`
	AckWait       time.Duration `yaml:"ack_wait"` // JetStream redelivery wait for commands.*
}

// BadgerConfig holds BadgerDB settings.
type BadgerConfig struct {
	DataDir              string        `yaml:"data_dir"`               // path to BadgerDB directory
	IdempotencyKeyTTL    time.Duration `yaml:"idempotency_key_ttl"`    // how long to retain processed keys
	GCIntervalSeconds    int           `yaml:"gc_interval_seconds"`    // value log GC interval
}

// ProxyConfig holds bybit-proxy HTTP client settings.
type ProxyConfig struct {
	BaseURL        string        `yaml:"base_url"`         // e.g. "http://127.0.0.1:8081"
	Timeout        time.Duration `yaml:"timeout"`
	MaxRetries     int           `yaml:"max_retries"`
	RetryBackoffMs int           `yaml:"retry_backoff_ms"`
}

// RiskGateConfig holds Risk Gate #2 rule thresholds.
// These are loaded from config/risk_gate_2.yaml independently of the orchestrator.
type RiskGateConfig struct {
	MaxPositionSizePct    float64  `yaml:"max_position_size_pct"`    // e.g. 0.25 (% of account)
	MaxOpenPositions      int      `yaml:"max_open_positions"`       // e.g. 3
	DailyDrawdownStopPct  float64  `yaml:"daily_drawdown_stop_pct"`  // e.g. 2.0
	LLMConfidenceMin      float64  `yaml:"llm_confidence_min"`       // e.g. 0.5
	MaxLeverage           float64  `yaml:"max_leverage"`             // e.g. 3.0
	InstrumentWhitelist   []string `yaml:"instrument_whitelist"`     // e.g. ["BTCUSDT","ETHUSDT"]
	MaxSlippageBPS        int      `yaml:"max_slippage_bps"`         // e.g. 30
	TickerCooldownMinutes int      `yaml:"ticker_cooldown_minutes"`  // minutes since last close

	// CorrelationMatrix: optional symbol→symbol→coefficient table.
	// If populated, Gate #2 rejects a new position whose symbol has an
	// existing open position with |corr| > MaxCorrelation. Leave empty to skip.
	CorrelationMatrix map[string]map[string]float64 `yaml:"correlation_matrix"`
	MaxCorrelation    float64                       `yaml:"max_correlation"` // e.g. 0.8
}

// DeadManConfig holds dead-man switch parameters.
type DeadManConfig struct {
	HeartbeatTimeoutMinutes int `yaml:"heartbeat_timeout_minutes"` // N minutes without brain heartbeat
	CheckIntervalSeconds    int `yaml:"check_interval_seconds"`
}

// MetricsConfig holds Prometheus metrics endpoint settings.
type MetricsConfig struct {
	Addr string `yaml:"addr"` // e.g. ":9090"
}

// HMACConfig holds the HMAC secret for command verification.
type HMACConfig struct {
	SecretFile string `yaml:"secret_file"` // path to file containing the raw secret
}

// Load reads the YAML config file and returns a parsed Config.
func Load(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read config %s: %w", path, err)
	}

	var cfg Config
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("parse config %s: %w", path, err)
	}

	if err := cfg.validate(); err != nil {
		return nil, fmt.Errorf("invalid config: %w", err)
	}

	return &cfg, nil
}

func (c *Config) validate() error {
	if c.NATS.URL == "" {
		return fmt.Errorf("nats.url is required")
	}
	if c.Badger.DataDir == "" {
		return fmt.Errorf("badger.data_dir is required")
	}
	if c.Proxy.BaseURL == "" {
		return fmt.Errorf("proxy.base_url is required")
	}
	if c.RiskGate.MaxOpenPositions <= 0 {
		return fmt.Errorf("risk_gate.max_open_positions must be > 0")
	}
	if c.DeadMan.HeartbeatTimeoutMinutes <= 0 {
		return fmt.Errorf("dead_man.heartbeat_timeout_minutes must be > 0")
	}
	if c.NATS.AckWait <= 0 {
		c.NATS.AckWait = 30 * time.Second
	}
	return nil
}

// LoadHMACSecret reads the HMAC shared secret from the configured file.
func (c *Config) LoadHMACSecret() ([]byte, error) {
	if c.HMAC.SecretFile == "" {
		return nil, fmt.Errorf("hmac.secret_file is required")
	}
	data, err := os.ReadFile(c.HMAC.SecretFile)
	if err != nil {
		return nil, fmt.Errorf("read hmac secret: %w", err)
	}
	// Trim trailing newline that editors may add
	for len(data) > 0 && (data[len(data)-1] == '\n' || data[len(data)-1] == '\r') {
		data = data[:len(data)-1]
	}
	if len(data) < 32 {
		return nil, fmt.Errorf("hmac secret too short (%d bytes, need ≥32)", len(data))
	}
	return data, nil
}
