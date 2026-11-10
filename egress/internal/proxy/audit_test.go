package proxy

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestAuditorBatchesAndKeepsWhatTheAPIRefused(t *testing.T) {
	sink := &memorySink{fail: true}
	a := &Auditor{Sink: sink, Batch: 2, Limit: 3}
	for i := range 4 {
		a.Add(Record{Run: "run-1", Host: "h", Port: i})
	}
	if a.Pending() != 3 {
		t.Fatalf("pending %d, want the limit 3", a.Pending())
	}
	a.Flush(context.Background())
	if a.Pending() != 3 || len(sink.all()) != 0 {
		t.Fatal("a refused batch was dropped")
	}
	sink.fail = false
	a.Flush(context.Background())
	got := sink.all()
	if a.Pending() != 0 || len(got) != 3 || got[0].Port != 1 {
		t.Fatalf("sent %+v, pending %d", got, a.Pending())
	}
}

func TestAuditorRunSendsOnItsInterval(t *testing.T) {
	sink := &memorySink{}
	a := &Auditor{Sink: sink, Interval: 20 * time.Millisecond}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { a.Run(ctx); close(done) }()
	a.Add(Record{Run: "run-1"})
	deadline := time.Now().Add(2 * time.Second)
	for len(sink.all()) == 0 && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	a.Add(Record{Run: "run-2"})
	cancel()
	<-done
	if len(sink.all()) != 2 {
		t.Fatalf("records %+v", sink.all())
	}
}

func TestAPIClient(t *testing.T) {
	var posted map[string][]Record
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer sa-token" {
			http.Error(w, "no", 401)
			return
		}
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/internal/egress/runs/run-1":
			_ = json.NewEncoder(w).Encode(Policy{Run: "run-1", Allow: []string{"pypi.org"},
				Model: &Route{URL: "https://api.anthropic.com", Headers: map[string]string{"x-api-key": "k"}}})
		case r.Method == http.MethodGet:
			http.Error(w, "not active", http.StatusNotFound)
		case r.URL.Path == "/internal/egress/records":
			_ = json.NewDecoder(r.Body).Decode(&posted)
			w.WriteHeader(http.StatusNoContent)
		}
	}))
	defer server.Close()
	token := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(token, []byte("sa-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	api := &API{Base: server.URL, TokenFile: token}
	policy, err := api.Policy(context.Background(), "run-1")
	if err != nil || policy.Model.Headers["x-api-key"] != "k" || !policy.Allows("pypi.org") {
		t.Fatalf("%+v %v", policy, err)
	}
	if _, err := api.Policy(context.Background(), "run-2"); !errors.Is(err, ErrNoRun) {
		t.Fatalf("an inactive run: %v", err)
	}
	if err := api.PostRecords(context.Background(), []Record{{Run: "run-1", Host: "pypi.org"}}); err != nil {
		t.Fatal(err)
	}
	if len(posted["records"]) != 1 || posted["records"][0].Host != "pypi.org" {
		t.Fatalf("posted %+v", posted)
	}
}

func TestPoliciesCacheAndRefresh(t *testing.T) {
	source := &fakePolicies{policy: &Policy{Allow: []string{"a"}}}
	p := &Policies{Source: source, TTL: 30 * time.Millisecond}
	src := &Source{UID: "u", Run: "run-1"}
	for range 3 {
		if _, err := p.Get(context.Background(), src); err != nil {
			t.Fatal(err)
		}
	}
	if source.calls != 1 {
		t.Fatalf("fetched %d times within the TTL", source.calls)
	}
	time.Sleep(40 * time.Millisecond)
	source.err = errors.New("api down")
	if got, err := p.Get(context.Background(), src); err != nil || got.Allow[0] != "a" {
		t.Fatalf("a failed refresh lost the policy: %v", err)
	}
	source.err = ErrNoRun
	time.Sleep(40 * time.Millisecond)
	if _, err := p.Get(context.Background(), src); !errors.Is(err, ErrNoRun) {
		t.Fatal("an ended run kept its policy")
	}
	p.Forget(func(string) bool { return false })
	if len(p.entries) != 0 {
		t.Fatal("Forget kept entries")
	}
}
