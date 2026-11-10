package proxy

import (
	"context"
	"net/http/httptest"
	"net/netip"
	"sync"
	"testing"
	"time"

	"github.com/musabeytekin/exradius/egress/internal/kube"
)

func mustAddr(s string) netip.Addr { return netip.MustParseAddr(s) }

func podFor(uid string) *kube.Pod {
	p := &kube.Pod{}
	p.Metadata.UID = uid
	return p
}

// fakePolicies answers every run with one policy.
type fakePolicies struct {
	mu     sync.Mutex
	policy *Policy
	err    error
	calls  int
}

func (f *fakePolicies) Policy(ctx context.Context, run string) (*Policy, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls++
	if f.err != nil {
		return nil, f.err
	}
	copied := *f.policy
	copied.Run = run
	return &copied, nil
}

// memorySink keeps the records it is sent.
type memorySink struct {
	mu      sync.Mutex
	records []Record
	fail    bool
}

func (m *memorySink) PostRecords(ctx context.Context, records []Record) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.fail {
		return context.DeadlineExceeded
	}
	m.records = append(m.records, records...)
	return nil
}

func (m *memorySink) all() []Record {
	m.mu.Lock()
	defer m.mu.Unlock()
	return append([]Record(nil), m.records...)
}

type harness struct {
	proxy    *Proxy
	sources  *Sources
	policies *fakePolicies
	sink     *memorySink
	forward  *httptest.Server
	reverse  *httptest.Server
}

// newHarness is a proxy whose only source is 127.0.0.1 (every test client), in phase,
// with loopback allowed as a private destination (the test upstreams listen there).
func newHarness(t *testing.T, phase Phase, policy *Policy) *harness {
	t.Helper()
	sources := NewSources()
	sources.Set(&Source{Namespace: "exradius-runs", Pod: "run-1-0", UID: "uid-1", Run: "run-1", Purpose: "task",
		Phase: phase, IPs: []string{"127.0.0.1", "::1"}})
	policies := &fakePolicies{policy: policy}
	sink := &memorySink{}
	allow, _ := ParsePrivateAllow("127.0.0.0/8,::1")
	p := &Proxy{
		Sources:    sources,
		Policies:   &Policies{Source: policies, TTL: time.Minute},
		Dialer:     &Dialer{AllowPrivate: allow},
		Audit:      &Auditor{Sink: sink, Batch: 1},
		LookupWait: 50 * time.Millisecond,
	}
	h := &harness{proxy: p, sources: sources, policies: policies, sink: sink}
	h.forward = httptest.NewServer(p.Forward())
	h.reverse = httptest.NewServer(p.Reverse())
	t.Cleanup(func() {
		h.forward.Close()
		h.reverse.Close()
	})
	return h
}

func (h *harness) records(t *testing.T) []Record {
	t.Helper()
	h.proxy.Audit.Flush(context.Background())
	return h.sink.all()
}

func (h *harness) setPhase(phase Phase) {
	h.sources.Set(&Source{Namespace: "exradius-runs", Pod: "run-1-0", UID: "uid-1", Run: "run-1", Purpose: "task",
		Phase: phase, IPs: []string{"127.0.0.1", "::1"}})
}

// waitRecords waits until at least n records arrived.
func (h *harness) waitRecords(t *testing.T, n int) []Record {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for {
		records := h.records(t)
		if len(records) >= n || time.Now().After(deadline) {
			return records
		}
		time.Sleep(10 * time.Millisecond)
	}
}
