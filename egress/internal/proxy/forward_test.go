package proxy

import (
	"bufio"
	"context"
	"crypto/tls"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"
)

// clientVia is an HTTP client whose every request goes through the forward proxy.
func clientVia(t *testing.T, forward string) *http.Client {
	t.Helper()
	proxyURL, _ := url.Parse(forward)
	return &http.Client{Timeout: 10 * time.Second, Transport: &http.Transport{
		Proxy:           http.ProxyURL(proxyURL),
		TLSClientConfig: &tls.Config{InsecureSkipVerify: true}, //nolint:gosec // test server
	}}
}

// connect sends a raw CONNECT and returns the status line.
func connect(t *testing.T, forward, target string) string {
	t.Helper()
	conn, err := net.Dial("tcp", strings.TrimPrefix(forward, "http://"))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	_, _ = io.WriteString(conn, "CONNECT "+target+" HTTP/1.1\r\nHost: "+target+"\r\n\r\n")
	line, err := bufio.NewReader(conn).ReadString('\n')
	if err != nil {
		t.Fatal(err)
	}
	return strings.TrimSpace(line)
}

func TestSetupPhaseTunnelsAnywhere(t *testing.T) {
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, "hello from upstream")
	}))
	defer upstream.Close()
	h := newHarness(t, PhaseSetup, &Policy{})
	client := clientVia(t, h.forward.URL)
	resp, err := client.Get(upstream.URL)
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if string(body) != "hello from upstream" {
		t.Fatalf("body %q", body)
	}
	// A tunnel is recorded when it closes.
	client.CloseIdleConnections()
	records := h.waitRecords(t, 1)
	if len(records) != 1 || records[0].Route != "connect" || !records[0].Allowed || records[0].Phase != PhaseSetup ||
		records[0].Run != "run-1" || records[0].BytesReceived == 0 || records[0].BytesSent == 0 {
		t.Fatalf("records %+v", records)
	}
	if h.policies.calls != 0 {
		t.Fatal("the setup phase read a policy it does not need")
	}
}

func TestAgentPhaseRefusesHostsOffTheList(t *testing.T) {
	h := newHarness(t, PhaseAgent, &Policy{Allow: []string{"registry.npmjs.org", "*.pythonhosted.org"}})
	line := connect(t, h.forward.URL, "example.com:443")
	want := "HTTP/1.1 403 example.com is not on this task's network allowlist (network.allow)"
	if line != want {
		t.Fatalf("status line %q, want %q", line, want)
	}
	// A client library shows the reason too.
	_, err := clientVia(t, h.forward.URL).Get("https://example.com/")
	if err == nil || !strings.Contains(err.Error(), "example.com is not on this task's network allowlist") {
		t.Fatalf("error %v", err)
	}
	records := h.records(t)
	if len(records) != 2 || records[0].Allowed || records[0].Host != "example.com" || records[0].Port != 443 ||
		!strings.Contains(records[0].Reason, "network allowlist") || records[0].Phase != PhaseAgent {
		t.Fatalf("records %+v", records)
	}
}

func TestAgentPhaseAllowsListedHosts(t *testing.T) {
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, "ok")
	}))
	defer upstream.Close()
	h := newHarness(t, PhaseAgent, &Policy{Allow: []string{"localhost"}})
	u, _ := url.Parse(upstream.URL)
	resp, err := clientVia(t, h.forward.URL).Get("https://localhost:" + u.Port() + "/")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("status %d", resp.StatusCode)
	}
	// Through *.domain, and not the domain itself.
	if !MatchHost([]string{"*.pythonhosted.org"}, "files.pythonhosted.org") || MatchHost([]string{"*.pythonhosted.org"}, "pythonhosted.org") {
		t.Fatal("wildcard matching")
	}
	if !MatchHost([]string{"PyPI.org"}, "pypi.org.") {
		t.Fatal("case and trailing dot")
	}
}

func TestInternalAddressesAreRefusedEvenInSetup(t *testing.T) {
	h := newHarness(t, PhaseSetup, &Policy{})
	h.proxy.Dialer.AllowPrivate = nil
	for _, target := range []string{"127.0.0.1:5432", "10.244.0.15:5432", "169.254.169.254:80", "[::1]:22"} {
		line := connect(t, h.forward.URL, target)
		if !strings.HasPrefix(line, "HTTP/1.1 403 ") || !strings.Contains(line, "private or cluster-internal") {
			t.Fatalf("%s: %q", target, line)
		}
	}
	line := connect(t, h.forward.URL, "localhost:5432")
	if !strings.Contains(line, "localhost resolves to a private or cluster-internal address") {
		t.Fatalf("a name resolving to loopback: %q", line)
	}
}

func TestPrivateAllowEntries(t *testing.T) {
	allow, err := ParsePrivateAllow("kind-registry:5000, 10.20.0.0/16,192.168.1.7")
	if err != nil {
		t.Fatal(err)
	}
	d := &Dialer{AllowPrivate: allow}
	cases := []struct {
		host string
		ip   string
		port int
		want bool
	}{
		{"kind-registry", "172.18.0.3", 5000, true},
		{"kind-registry", "172.18.0.3", 6443, false},
		{"mirror.corp", "10.20.3.4", 443, true},
		{"x", "10.21.0.1", 443, false},
		{"x", "192.168.1.7", 22, true},
	}
	for _, c := range cases {
		if got := d.allowedPrivate(c.host, mustAddr(c.ip), c.port); got != c.want {
			t.Errorf("%s %s:%d = %v", c.host, c.ip, c.port, got)
		}
	}
	if _, err := ParsePrivateAllow("host:notaport"); err == nil {
		t.Fatal("a bad port was accepted")
	}
}

func TestUnknownSourcesAreRefused(t *testing.T) {
	h := newHarness(t, PhaseSetup, &Policy{})
	h.sources.Event("DELETED", podFor("uid-1"))
	line := connect(t, h.forward.URL, "example.com:443")
	if !strings.Contains(line, "come from no run or build pod") {
		t.Fatalf("%q", line)
	}
	if len(h.records(t)) != 0 {
		t.Fatal("a request without a run was sent to the api")
	}
}

func TestPlainHTTPIsForwardedAndDecided(t *testing.T) {
	var seen http.Header
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = r.Header.Clone()
		_, _ = io.WriteString(w, "plain "+r.URL.Path)
	}))
	defer upstream.Close()
	h := newHarness(t, PhaseSetup, &Policy{})
	req, _ := http.NewRequest(http.MethodGet, upstream.URL+"/ubuntu/dists?x=1", nil)
	req.Header.Set("Proxy-Authorization", "Basic c2VjcmV0")
	resp, err := clientVia(t, h.forward.URL).Do(req)
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if string(body) != "plain /ubuntu/dists" || seen.Get("Proxy-Authorization") != "" {
		t.Fatalf("body %q, headers %v", body, seen)
	}
	h.setPhase(PhaseAgent)
	resp, err = clientVia(t, h.forward.URL).Get("http://archive.ubuntu.com/ubuntu/")
	if err != nil {
		t.Fatal(err)
	}
	body, _ = io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 403 || !strings.Contains(string(body), "archive.ubuntu.com is not on this task's network allowlist") {
		t.Fatalf("agent phase plain HTTP: %d %q", resp.StatusCode, body)
	}
	records := h.records(t)
	if len(records) != 2 || records[0].Route != "http" || records[0].Path != "/ubuntu/dists" || records[0].Status != 200 ||
		records[1].Allowed {
		t.Fatalf("records %+v", records)
	}
}

func TestUpstreamProxyChaining(t *testing.T) {
	target := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, "through the corporate proxy")
	}))
	defer target.Close()
	var mu sync.Mutex
	var connects []string
	var auth string
	corporate := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodConnect {
			http.Error(w, "connect only", 405)
			return
		}
		mu.Lock()
		connects = append(connects, r.Host)
		auth = r.Header.Get("Proxy-Authorization")
		mu.Unlock()
		dest, err := net.Dial("tcp", r.Host)
		if err != nil {
			http.Error(w, err.Error(), 502)
			return
		}
		conn, _, _ := http.NewResponseController(w).Hijack()
		_, _ = io.WriteString(conn, "HTTP/1.1 200 OK\r\n\r\n")
		go func() { _, _ = io.Copy(dest, conn); dest.Close() }()
		_, _ = io.Copy(conn, dest)
		conn.Close()
	}))
	defer corporate.Close()
	h := newHarness(t, PhaseSetup, &Policy{})
	upstreamURL, _ := url.Parse(corporate.URL)
	upstreamURL.User = url.UserPassword("svc", "pw")
	h.proxy.Dialer.Upstream = upstreamURL
	resp, err := clientVia(t, h.forward.URL).Get(target.URL)
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	mu.Lock()
	defer mu.Unlock()
	if string(body) != "through the corporate proxy" || len(connects) != 1 || auth != "Basic c3ZjOnB3" {
		t.Fatalf("body %q, connects %v, auth %q", body, connects, auth)
	}
}

func TestAPolicyThatCannotBeReadRefusesTheAgentPhase(t *testing.T) {
	h := newHarness(t, PhaseAgent, &Policy{})
	h.policies.err = ErrNoRun
	line := connect(t, h.forward.URL, "pypi.org:443")
	if !strings.Contains(line, "the run of this pod is not active") {
		t.Fatalf("%q", line)
	}
	h.policies.err = context.DeadlineExceeded
	line = connect(t, h.forward.URL, "pypi.org:443")
	if !strings.Contains(line, "could not be read") {
		t.Fatalf("%q", line)
	}
}
