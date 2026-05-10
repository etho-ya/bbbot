# Execution Engine

"The hands" of the Trading Brain. Single Go service that runs on the VPS under
systemd and is the **only** component allowed to place live orders on Bybit.

See the full architectural specification in the companion [Wiki](../Wiki/wiki/):

- [entities/execution-engine](../Wiki/wiki/entities/execution-engine.md) — this service
- [concepts/command-contract](../Wiki/wiki/concepts/command-contract.md) — the inbound command format
- [concepts/risk-gates](../Wiki/wiki/concepts/risk-gates.md) — Gate #2 rules
- [concepts/state-management](../Wiki/wiki/concepts/state-management.md) — recovery protocol
- [concepts/nats-message-bus](../Wiki/wiki/concepts/nats-message-bus.md) — NATS topology
- [concepts/security-model](../Wiki/wiki/concepts/security-model.md) — mTLS + HMAC + audit

## Responsibilities

1. Subscribe to `commands.order.*` / `commands.position.*` on NATS JetStream
   with explicit ACK and `MaxDeliver=3`.
2. Validate each command: schema version, TTL, HMAC-SHA256 signature,
   idempotency key dedup (BadgerDB), Risk Gate #2 rules.
3. Forward valid orders to `bybit-proxy` via internal HTTP.
4. Publish order and position lifecycle events to `events.*`.
5. Run the dead-man switch: if `heartbeat.brain` is silent for N minutes while
   positions are open, emergency-close them and emit a critical alert.
6. Reconcile local state with Bybit on startup (recovery protocol).

## Layout

```
cmd/engine/              main.go — startup, recovery, graceful shutdown
config/                  config.yaml + .hmac-secret (dev only; prod via Vault)
internal/config/         typed YAML config + HMAC secret loader
internal/domain/         Command envelope, payloads, events, HMAC, TTL
internal/risk/           Risk Gate #2: independent rule evaluation
internal/state/          BadgerDB wrapper (idempotency keys, positions, orders)
internal/proxy/          HTTP client for bybit-proxy
internal/transport/      NATS JetStream subscribe/publish
internal/engine/         coordination: handleCommand, lifecycle, deadman, recover
internal/metrics/        Prometheus counters / gauges / histograms
deploy/                  systemd unit
```

## Running locally

```bash
# Dev fixture secret (≥32 bytes) must exist before startup:
#   config/.hmac-secret
go build -o bin/engine ./cmd/engine
./bin/engine -config config/config.yaml
```

A local NATS JetStream + a `bybit-proxy` stub on `127.0.0.1:8080` are required
for the engine to start. The recovery step calls `GET /api/v1/position/list`
and `/api/v1/order/open` on the proxy before subscribing to commands.

## Tests

```bash
go test ./...
```

Covers the Command Contract (HMAC, TTL, schema), every Gate #2 rule, and the
dead-man switch state machine. End-to-end NATS tests require a running broker
and are not part of the default unit suite.

## Metrics

Prometheus endpoint on `cfg.metrics.addr` (default `:9090`). Custom series:

- `execution_engine_commands_received_total{type}`
- `execution_engine_commands_rejected_total{stage,reason}`
- `execution_engine_orders_placed_total{symbol,side}`
- `execution_engine_order_place_latency_seconds`
- `execution_engine_gate2_rejections_total{rule}`
- `execution_engine_deadman_triggered`
- `execution_engine_brain_heartbeat_age_seconds`
- `execution_engine_open_positions`
- `execution_engine_kill_received`

## Deployment

Systemd unit lives in [`deploy/execution-engine.service`](deploy/execution-engine.service).
Secrets (`.hmac-secret`, mTLS client cert/key/CA, NATS creds) are injected by
Ansible from Vault / SOPS-age at provisioning time — never committed to git
(see [security-model](../Wiki/wiki/concepts/security-model.md)).
