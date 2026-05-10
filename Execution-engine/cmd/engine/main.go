package main

import (
	"context"
	"flag"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/prometheus/client_golang/prometheus/promhttp"

	"github.com/Crypto-Baron/execution-engine/internal/config"
	"github.com/Crypto-Baron/execution-engine/internal/engine"
	"github.com/Crypto-Baron/execution-engine/internal/proxy"
	"github.com/Crypto-Baron/execution-engine/internal/risk"
	"github.com/Crypto-Baron/execution-engine/internal/state"
	"github.com/Crypto-Baron/execution-engine/internal/transport"
)

func main() {
	configPath := flag.String("config", "config/config.yaml", "Path to config file")
	flag.Parse()

	// Initialize structured logger
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
		Level: slog.LevelInfo,
	}))
	slog.SetDefault(logger)

	// Load config
	cfg, err := config.Load(*configPath)
	if err != nil {
		logger.Error("failed to load config", "error", err)
		os.Exit(1)
	}

	// Load HMAC Secret
	hmacSecret, err := cfg.LoadHMACSecret()
	if err != nil {
		logger.Error("failed to load hmac secret", "error", err)
		os.Exit(1)
	}

	// Initialize BadgerDB
	store, err := state.NewStore(cfg.Badger.DataDir, cfg.Badger.IdempotencyKeyTTL, logger)
	if err != nil {
		logger.Error("failed to init badgerdb", "error", err)
		os.Exit(1)
	}
	defer store.Close()

	// Start Badger GC
	go func() {
		ticker := time.NewTicker(time.Duration(cfg.Badger.GCIntervalSeconds) * time.Second)
		for range ticker.C {
			store.RunGC()
		}
	}()

	// Initialize NATS Bus
	bus, err := transport.NewBus(cfg.NATS, logger)
	if err != nil {
		logger.Error("failed to connect to nats", "error", err)
		os.Exit(1)
	}
	defer bus.Close()

	// Initialize Proxy Client
	proxyClient := proxy.NewClient(cfg.Proxy, logger)

	// Initialize Risk Gate #2
	gate := risk.NewGate(cfg.RiskGate, store, logger)

	// Initialize Dead-Man Switch
	deadman := engine.NewDeadManSwitch(
		cfg.DeadMan.HeartbeatTimeoutMinutes,
		cfg.DeadMan.CheckIntervalSeconds,
		store,
		bus,
		proxyClient,
		logger,
	)

	// Initialize Execution Engine
	execEngine := engine.New(cfg, store, bus, proxyClient, gate, deadman, hmacSecret, logger)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Start Metrics Server
	go func() {
		http.Handle("/metrics", promhttp.Handler())
		logger.Info("starting metrics server", "addr", cfg.Metrics.Addr)
		if err := http.ListenAndServe(cfg.Metrics.Addr, nil); err != nil {
			logger.Error("metrics server failed", "error", err)
		}
	}()

	// Recovery — reconcile local state with Bybit before accepting commands.
	// See Wiki: concepts/state-management.md. Target: < 30s (Phase 4).
	recoverCtx, recoverCancel := context.WithTimeout(ctx, 30*time.Second)
	if err := execEngine.Recover(recoverCtx); err != nil {
		recoverCancel()
		logger.Error("engine: recovery failed, refusing to start", "error", err)
		os.Exit(1)
	}
	recoverCancel()

	// Start Engine
	if err := execEngine.Start(ctx); err != nil {
		logger.Error("engine failed to start", "error", err)
		os.Exit(1)
	}

	logger.Info("execution-engine running")

	// Wait for shutdown signal
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	s := <-sig

	logger.Info("received shutdown signal, shutting down", "signal", s.String())
	cancel()

	// Give time for graceful shutdown
	time.Sleep(1 * time.Second)
}
