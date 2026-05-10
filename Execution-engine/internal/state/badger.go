// Package state wraps BadgerDB for local state persistence.
// Used for idempotency key dedup, position snapshots, and order tracking.
// See Wiki: concepts/state-management.md — execution-engine is the source of
// truth for open positions and idempotency keys.
package state

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"time"

	"github.com/dgraph-io/badger/v4"

	"github.com/Crypto-Baron/execution-engine/internal/domain"
)

const (
	prefixIdemKey   = "idem:"      // idempotency keys
	prefixPosition  = "pos:"       // tracked positions
	prefixOrder     = "ord:"       // tracked orders
	prefixMeta      = "meta:"      // metadata (e.g. last NATS sequence)
)

// Store wraps BadgerDB with typed accessors.
type Store struct {
	db             *badger.DB
	idempotencyTTL time.Duration
	logger         *slog.Logger
}

// NewStore opens or creates a BadgerDB at the given path.
func NewStore(dataDir string, idempotencyTTL time.Duration, logger *slog.Logger) (*Store, error) {
	opts := badger.DefaultOptions(dataDir).
		WithLogger(nil). // BadgerDB's own logging is too noisy; we use slog
		WithValueLogFileSize(64 << 20) // 64MB value log files

	db, err := badger.Open(opts)
	if err != nil {
		return nil, fmt.Errorf("open badger at %s: %w", dataDir, err)
	}

	s := &Store{
		db:             db,
		idempotencyTTL: idempotencyTTL,
		logger:         logger,
	}

	return s, nil
}

// Close flushes and closes the database.
func (s *Store) Close() error {
	return s.db.Close()
}

// ---------------------------------------------------------------------------
// Idempotency
// ---------------------------------------------------------------------------

// HasIdempotencyKey returns true if the key has already been processed.
func (s *Store) HasIdempotencyKey(key string) (bool, error) {
	var found bool
	err := s.db.View(func(txn *badger.Txn) error {
		_, err := txn.Get([]byte(prefixIdemKey + key))
		if err == badger.ErrKeyNotFound {
			return nil
		}
		if err != nil {
			return err
		}
		found = true
		return nil
	})
	return found, err
}

// StoreIdempotencyKey records a processed idempotency key with TTL.
func (s *Store) StoreIdempotencyKey(key string) error {
	return s.db.Update(func(txn *badger.Txn) error {
		entry := badger.NewEntry([]byte(prefixIdemKey+key), []byte("1")).
			WithTTL(s.idempotencyTTL)
		return txn.SetEntry(entry)
	})
}

// ---------------------------------------------------------------------------
// Position tracking
// ---------------------------------------------------------------------------

// SavePosition persists a tracked position.
func (s *Store) SavePosition(pos *domain.TrackedPosition) error {
	data, err := json.Marshal(pos)
	if err != nil {
		return fmt.Errorf("marshal position: %w", err)
	}
	return s.db.Update(func(txn *badger.Txn) error {
		return txn.Set([]byte(prefixPosition+pos.Symbol), data)
	})
}

// GetPosition retrieves a tracked position by symbol.
func (s *Store) GetPosition(symbol string) (*domain.TrackedPosition, error) {
	var pos domain.TrackedPosition
	err := s.db.View(func(txn *badger.Txn) error {
		item, err := txn.Get([]byte(prefixPosition + symbol))
		if err != nil {
			return err
		}
		return item.Value(func(val []byte) error {
			return json.Unmarshal(val, &pos)
		})
	})
	if err == badger.ErrKeyNotFound {
		return nil, nil
	}
	return &pos, err
}

// DeletePosition removes a tracked position.
func (s *Store) DeletePosition(symbol string) error {
	return s.db.Update(func(txn *badger.Txn) error {
		return txn.Delete([]byte(prefixPosition + symbol))
	})
}

// ListPositions returns all currently tracked positions.
func (s *Store) ListPositions() ([]*domain.TrackedPosition, error) {
	var positions []*domain.TrackedPosition
	err := s.db.View(func(txn *badger.Txn) error {
		opts := badger.DefaultIteratorOptions
		opts.Prefix = []byte(prefixPosition)
		it := txn.NewIterator(opts)
		defer it.Close()

		for it.Rewind(); it.Valid(); it.Next() {
			item := it.Item()
			err := item.Value(func(val []byte) error {
				var pos domain.TrackedPosition
				if err := json.Unmarshal(val, &pos); err != nil {
					return err
				}
				positions = append(positions, &pos)
				return nil
			})
			if err != nil {
				return err
			}
		}
		return nil
	})
	return positions, err
}

// ---------------------------------------------------------------------------
// Order tracking
// ---------------------------------------------------------------------------

// SaveOrder persists a tracked order.
func (s *Store) SaveOrder(order *domain.TrackedOrder) error {
	data, err := json.Marshal(order)
	if err != nil {
		return fmt.Errorf("marshal order: %w", err)
	}
	return s.db.Update(func(txn *badger.Txn) error {
		return txn.Set([]byte(prefixOrder+order.ClientOrderID), data)
	})
}

// GetOrder retrieves a tracked order by client order ID.
func (s *Store) GetOrder(clientOrderID string) (*domain.TrackedOrder, error) {
	var order domain.TrackedOrder
	err := s.db.View(func(txn *badger.Txn) error {
		item, err := txn.Get([]byte(prefixOrder + clientOrderID))
		if err != nil {
			return err
		}
		return item.Value(func(val []byte) error {
			return json.Unmarshal(val, &order)
		})
	})
	if err == badger.ErrKeyNotFound {
		return nil, nil
	}
	return &order, err
}

// DeleteOrder removes a tracked order.
func (s *Store) DeleteOrder(clientOrderID string) error {
	return s.db.Update(func(txn *badger.Txn) error {
		return txn.Delete([]byte(prefixOrder + clientOrderID))
	})
}

// ---------------------------------------------------------------------------
// Metadata (e.g. last processed NATS sequence)
// ---------------------------------------------------------------------------

// SetMeta stores a metadata key-value pair.
func (s *Store) SetMeta(key string, value []byte) error {
	return s.db.Update(func(txn *badger.Txn) error {
		return txn.Set([]byte(prefixMeta+key), value)
	})
}

// GetMeta retrieves a metadata value.
func (s *Store) GetMeta(key string) ([]byte, error) {
	var val []byte
	err := s.db.View(func(txn *badger.Txn) error {
		item, err := txn.Get([]byte(prefixMeta + key))
		if err != nil {
			return err
		}
		return item.Value(func(v []byte) error {
			val = make([]byte, len(v))
			copy(val, v)
			return nil
		})
	})
	if err == badger.ErrKeyNotFound {
		return nil, nil
	}
	return val, err
}

// ---------------------------------------------------------------------------
// Maintenance
// ---------------------------------------------------------------------------

// RunGC triggers BadgerDB value log garbage collection.
func (s *Store) RunGC() {
	for {
		err := s.db.RunValueLogGC(0.5)
		if err != nil {
			break
		}
	}
}
