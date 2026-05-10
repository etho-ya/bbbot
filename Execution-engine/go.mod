module github.com/Crypto-Baron/execution-engine

go 1.24.0

toolchain go1.24.4

require (
	github.com/dgraph-io/badger/v4 v4.9.1
	github.com/nats-io/nats.go v1.47.0
	github.com/prometheus/client_golang v1.23.2
	gopkg.in/yaml.v3 v3.0.1
)

require (
	github.com/beorn7/perks v1.0.1 // indirect
	github.com/cespare/xxhash/v2 v2.3.0 // indirect
	github.com/dgraph-io/ristretto/v2 v2.2.0 // indirect
	github.com/dustin/go-humanize v1.0.1 // indirect
	github.com/go-logr/logr v1.4.3 // indirect
	github.com/go-logr/stdr v1.2.2 // indirect
	github.com/google/flatbuffers v25.2.10+incompatible // indirect
	github.com/klauspost/compress v1.18.5 // indirect
	github.com/munnerz/goautoneg v0.0.0-20191010083416-a7dc8b61c822 // indirect
	github.com/nats-io/nkeys v0.4.15 // indirect
	github.com/nats-io/nuid v1.0.1 // indirect
	github.com/prometheus/client_model v0.6.2 // indirect
	github.com/prometheus/common v0.66.1 // indirect
	github.com/prometheus/procfs v0.16.1 // indirect
	go.opentelemetry.io/auto/sdk v1.1.0 // indirect
	go.opentelemetry.io/otel v1.37.0 // indirect
	go.opentelemetry.io/otel/metric v1.37.0 // indirect
	go.opentelemetry.io/otel/trace v1.37.0 // indirect
	go.yaml.in/yaml/v2 v2.4.2 // indirect
	golang.org/x/crypto v0.49.0 // indirect
	golang.org/x/net v0.51.0 // indirect
	golang.org/x/sys v0.42.0 // indirect
	google.golang.org/protobuf v1.36.8 // indirect
)

// Transitive deps from badger/prometheus previously resolved to versions that
// require Go 1.25+. Pin x/* to builds compatible with Go 1.23–1.24 toolchains.
replace (
	golang.org/x/crypto => golang.org/x/crypto v0.28.0
	golang.org/x/net => golang.org/x/net v0.28.0
	golang.org/x/sys => golang.org/x/sys v0.28.0
)
