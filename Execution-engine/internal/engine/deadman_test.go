package engine

import (
	"io"
	"log/slog"
	"testing"
	"time"

	"github.com/Crypto-Baron/execution-engine/internal/domain"
	"github.com/Crypto-Baron/execution-engine/internal/state"
)

func newLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func newTestStore(t *testing.T) *state.Store {
	t.Helper()
	s, err := state.NewStore(t.TempDir(), time.Hour, newLogger())
	if err != nil {
		t.Fatalf("store: %v", err)
	}
	t.Cleanup(func() { s.Close() })
	return s
}

func TestDeadMan_RecordResetsTrigger(t *testing.T) {
	d := NewDeadManSwitch(1, 1, newTestStore(t), nil, nil, newLogger())
	d.triggered.Store(true)
	d.RecordHeartbeat(time.Now())
	if d.IsTriggered() {
		t.Errorf("heartbeat should have reset triggered flag")
	}
}

func TestDeadMan_SecondsSinceHeartbeat(t *testing.T) {
	d := NewDeadManSwitch(1, 1, newTestStore(t), nil, nil, newLogger())
	d.RecordHeartbeat(time.Now().Add(-5 * time.Second))
	age := d.SecondsSinceHeartbeat()
	if age < 4 || age > 10 {
		t.Errorf("expected ~5s age, got %f", age)
	}
}

func TestDeadMan_NoPositions_NoTrigger(t *testing.T) {
	store := newTestStore(t)
	d := NewDeadManSwitch(1, 1, store, nil, nil, newLogger())
	d.lastHeartbeat = time.Now().Add(-10 * time.Minute) // stale
	// check() early-returns when positions list is empty; triggered stays false.
	d.check(nil)
	if d.IsTriggered() {
		t.Errorf("no positions → no trigger")
	}
}

func TestDeadMan_StorePosition_VisibleToList(t *testing.T) {
	// Ensures our assumptions about state.Store used by the deadman are correct.
	store := newTestStore(t)
	if err := store.SavePosition(&domain.TrackedPosition{Symbol: "BTCUSDT", Side: "Buy", Size: "1"}); err != nil {
		t.Fatalf("save: %v", err)
	}
	positions, err := store.ListPositions()
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	if len(positions) != 1 {
		t.Errorf("expected 1 position, got %d", len(positions))
	}
}
