package proxy

import (
	"context"
	"log/slog"
	"sync"
	"time"
)

// Record is one request, as the api stores it in egress_records.
type Record struct {
	Run   string `json:"run"`
	Phase Phase  `json:"phase"`
	// Route is connect (a tunnel), http (a plain HTTP request through the forward proxy),
	// model, git or mcp (the reverse routes).
	Route         string    `json:"route"`
	Method        string    `json:"method,omitempty"`
	Host          string    `json:"host"`
	Port          int       `json:"port"`
	Path          string    `json:"path,omitempty"`
	Allowed       bool      `json:"allowed"`
	Reason        string    `json:"reason,omitempty"`
	Status        int       `json:"status,omitempty"`
	BytesSent     int64     `json:"bytes_sent"`
	BytesReceived int64     `json:"bytes_received"`
	DurationMs    int64     `json:"duration_ms"`
	At            time.Time `json:"at"`
}

// RecordSink receives batches of records; *API is the real one.
type RecordSink interface {
	PostRecords(ctx context.Context, records []Record) error
}

// Auditor batches records to the api: every Interval or once Batch records are waiting.
// A batch the api does not take is kept for the next try, up to Limit records.
type Auditor struct {
	Sink     RecordSink
	Interval time.Duration
	Batch    int
	Limit    int
	Log      *slog.Logger

	mu      sync.Mutex
	pending []Record
	kick    chan struct{}
	once    sync.Once
}

func (a *Auditor) init() {
	a.once.Do(func() {
		a.kick = make(chan struct{}, 1)
		if a.Interval == 0 {
			a.Interval = 2 * time.Second
		}
		if a.Batch == 0 {
			a.Batch = 200
		}
		if a.Limit == 0 {
			a.Limit = 20000
		}
	})
}

// Add queues a record.
func (a *Auditor) Add(r Record) {
	a.init()
	a.mu.Lock()
	a.pending = append(a.pending, r)
	if over := len(a.pending) - a.Limit; over > 0 {
		a.pending = a.pending[over:]
		if a.Log != nil {
			a.Log.Warn("audit backlog full, dropped the oldest records", "dropped", over)
		}
	}
	full := len(a.pending) >= a.Batch
	a.mu.Unlock()
	if full {
		select {
		case a.kick <- struct{}{}:
		default:
		}
	}
}

// Run sends batches until ctx ends, then sends what is left once.
func (a *Auditor) Run(ctx context.Context) {
	a.init()
	ticker := time.NewTicker(a.Interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			final, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			a.Flush(final)
			cancel()
			return
		case <-ticker.C:
		case <-a.kick:
		}
		a.Flush(ctx)
	}
}

// Flush sends everything pending, in batches.
func (a *Auditor) Flush(ctx context.Context) {
	a.init()
	for {
		a.mu.Lock()
		n := min(len(a.pending), a.Batch)
		batch := append([]Record(nil), a.pending[:n]...)
		a.mu.Unlock()
		if n == 0 {
			return
		}
		if err := a.Sink.PostRecords(ctx, batch); err != nil {
			if a.Log != nil {
				a.Log.Warn("could not send audit records, keeping them", "records", n, "error", err)
			}
			return
		}
		a.mu.Lock()
		a.pending = a.pending[n:]
		a.mu.Unlock()
	}
}

// Pending is the number of records not sent yet.
func (a *Auditor) Pending() int {
	a.mu.Lock()
	defer a.mu.Unlock()
	return len(a.pending)
}
