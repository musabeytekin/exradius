package kube

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"sync"
	"testing"
	"time"
)

type recorder struct {
	mu     sync.Mutex
	syncs  [][]string
	events []string
	done   chan struct{}
	want   int
}

func (r *recorder) Sync(namespace string, pods []*Pod) {
	r.mu.Lock()
	defer r.mu.Unlock()
	var names []string
	for _, p := range pods {
		names = append(names, p.Metadata.Name)
	}
	r.syncs = append(r.syncs, names)
	r.check()
}

func (r *recorder) Event(t EventType, pod *Pod) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.events = append(r.events, fmt.Sprintf("%s %s %s", t, pod.Metadata.Name, pod.Status.PodIP))
	r.check()
}

func (r *recorder) check() {
	if len(r.syncs)+len(r.events) == r.want {
		close(r.done)
	}
}

func podJSON(name, ip, version string) string {
	return fmt.Sprintf(`{"metadata":{"name":%q,"namespace":"exradius-runs","uid":"u-%s","resourceVersion":%q,
		"labels":{"exradius.dev/run":"run-1"}},"status":{"podIP":%q}}`, name, name, version, ip)
}

func TestWatchListsWatchesAndRelistsAfterExpiry(t *testing.T) {
	var mu sync.Mutex
	watches := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer tok" || r.URL.Query().Get("labelSelector") != "exradius.dev/run" {
			http.Error(w, "forbidden", 403)
			return
		}
		if r.URL.Query().Get("watch") == "" {
			_, _ = io.WriteString(w, `{"metadata":{"resourceVersion":"10"},"items":[`+podJSON("a", "10.0.0.1", "9")+`]}`)
			return
		}
		mu.Lock()
		watches++
		n := watches
		mu.Unlock()
		switch n {
		case 1:
			if r.URL.Query().Get("resourceVersion") != "10" {
				t.Errorf("first watch from %q", r.URL.Query().Get("resourceVersion"))
			}
			_, _ = io.WriteString(w, `{"type":"ADDED","object":`+podJSON("b", "10.0.0.2", "11")+"}\n")
			_, _ = io.WriteString(w, `{"type":"BOOKMARK","object":{"metadata":{"resourceVersion":"12"}}}`+"\n")
		case 2:
			if r.URL.Query().Get("resourceVersion") != "12" {
				t.Errorf("second watch from %q", r.URL.Query().Get("resourceVersion"))
			}
			_, _ = io.WriteString(w, `{"type":"ERROR","object":{"kind":"Status","code":410}}`+"\n")
		default:
			_, _ = io.WriteString(w, `{"type":"DELETED","object":`+podJSON("a", "10.0.0.1", "20")+"}\n")
			w.(http.Flusher).Flush()
			<-r.Context().Done()
		}
	}))
	defer server.Close()
	tokenFile := t.TempDir() + "/token"
	if err := writeFile(tokenFile, "tok\n"); err != nil {
		t.Fatal(err)
	}
	client := &Client{Base: server.URL, TokenFile: tokenFile, HTTP: server.Client()}
	// list, ADDED b, (410) relist, DELETED a
	rec := &recorder{done: make(chan struct{}), want: 4}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go client.WatchPods(ctx, "exradius-runs", "exradius.dev/run", rec, slog.New(slog.DiscardHandler))
	select {
	case <-rec.done:
	case <-time.After(5 * time.Second):
		rec.mu.Lock()
		defer rec.mu.Unlock()
		t.Fatalf("timed out: syncs %v events %v", rec.syncs, rec.events)
	}
	rec.mu.Lock()
	defer rec.mu.Unlock()
	if fmt.Sprint(rec.syncs) != "[[a] [a]]" || fmt.Sprint(rec.events) != "[ADDED b 10.0.0.2 DELETED a 10.0.0.1]" {
		t.Fatalf("syncs %v events %v", rec.syncs, rec.events)
	}
}

func writeFile(path, content string) error { return os.WriteFile(path, []byte(content), 0o600) }
