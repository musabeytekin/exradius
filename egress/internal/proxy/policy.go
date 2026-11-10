package proxy

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"
)

// Policy is what a run may reach and with which credentials, from the api's
// GET /internal/egress/runs/{run}. It is kept in memory only, for the pod's life.
type Policy struct {
	Run string `json:"run"`
	// Allow is the agent phase's host list (exact names, or *.domain for subdomains):
	// the install's egress.allow, the config's network.allow, the catalog hosts and the
	// hosts of declared HTTP connections.
	Allow []string `json:"allow"`
	// Model is the model route, /anthropic/: absent for runs without a provider.
	Model *Route `json:"model"`
	// Repositories are the git routes, /git/<host>/<path>.
	Repositories []Repository `json:"repositories"`
	// Connections are the HTTP connections whose headers hold a secret, /mcp/<name>.
	Connections []Connection `json:"connections"`
}

// Route is an upstream base URL and the headers the proxy sets on every request to it.
type Route struct {
	URL     string            `json:"url"`
	Headers map[string]string `json:"headers"`
}

// Repository is one repository of the run.
type Repository struct {
	URL     string            `json:"url"`
	Push    bool              `json:"push"`
	Headers map[string]string `json:"headers"`
}

// Connection is one HTTP connection of the run's agent.
type Connection struct {
	Name    string            `json:"name"`
	URL     string            `json:"url"`
	Headers map[string]string `json:"headers"`
}

// Allows reports whether host is on the policy's agent-phase list.
func (p *Policy) Allows(host string) bool { return MatchHost(p.Allow, host) }

// MatchHost reports whether host matches one of the patterns: an exact name (case does
// not matter) or *.domain, which matches every name below domain but not domain itself.
func MatchHost(patterns []string, host string) bool {
	host = strings.TrimSuffix(strings.ToLower(host), ".")
	for _, pattern := range patterns {
		pattern = strings.TrimSuffix(strings.ToLower(strings.TrimSpace(pattern)), ".")
		if pattern == "" {
			continue
		}
		if suffix, ok := strings.CutPrefix(pattern, "*."); ok {
			if strings.HasSuffix(host, "."+suffix) {
				return true
			}
		} else if host == pattern {
			return true
		}
	}
	return false
}

// API is the api's internal port, authenticated by the proxy's ServiceAccount token
// (audience exradius-api), which the api checks with a TokenReview.
type API struct {
	Base      string
	TokenFile string
	HTTP      *http.Client

	mu     sync.Mutex
	token  string
	readAt time.Time
}

func (a *API) bearer() (string, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.TokenFile == "" {
		return "", nil
	}
	if a.token == "" || time.Since(a.readAt) > time.Minute {
		data, err := os.ReadFile(a.TokenFile)
		if err != nil {
			return "", err
		}
		a.token, a.readAt = strings.TrimSpace(string(data)), time.Now()
	}
	return a.token, nil
}

func (a *API) do(ctx context.Context, method, path string, body any) (*http.Response, error) {
	var reader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		reader = bytes.NewReader(data)
	}
	req, err := http.NewRequestWithContext(ctx, method, strings.TrimRight(a.Base, "/")+path, reader)
	if err != nil {
		return nil, err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	token, err := a.bearer()
	if err != nil {
		return nil, fmt.Errorf("reading the ServiceAccount token: %w", err)
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	client := a.HTTP
	if client == nil {
		client = &http.Client{Timeout: 30 * time.Second, Transport: &http.Transport{Proxy: nil}}
	}
	return client.Do(req)
}

// ErrNoRun is the api's answer for a run that is not active (ended, or unknown).
var ErrNoRun = errors.New("the run is not active")

// Policy fetches a run's policy.
func (a *API) Policy(ctx context.Context, run string) (*Policy, error) {
	resp, err := a.do(ctx, http.MethodGet, "/internal/egress/runs/"+url.PathEscape(run), nil)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	switch resp.StatusCode {
	case http.StatusOK:
	case http.StatusNotFound:
		return nil, ErrNoRun
	default:
		detail, _ := io.ReadAll(io.LimitReader(resp.Body, 2048))
		return nil, fmt.Errorf("the api answered %d: %s", resp.StatusCode, strings.TrimSpace(string(detail)))
	}
	var policy Policy
	if err := json.NewDecoder(resp.Body).Decode(&policy); err != nil {
		return nil, err
	}
	return &policy, nil
}

// PostRecords sends a batch of audit records.
func (a *API) PostRecords(ctx context.Context, records []Record) error {
	resp, err := a.do(ctx, http.MethodPost, "/internal/egress/records", map[string]any{"records": records})
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		detail, _ := io.ReadAll(io.LimitReader(resp.Body, 2048))
		return fmt.Errorf("the api answered %d: %s", resp.StatusCode, strings.TrimSpace(string(detail)))
	}
	return nil
}

// PolicySource fetches policies; *API is the real one.
type PolicySource interface {
	Policy(ctx context.Context, run string) (*Policy, error)
}

// Policies caches each pod's policy for TTL, then fetches it again (a provider's
// credential may have been replaced). A failed refresh keeps the last one.
type Policies struct {
	Source PolicySource
	TTL    time.Duration

	mu      sync.Mutex
	entries map[string]*policyEntry
}

type policyEntry struct {
	mu        sync.Mutex
	policy    *Policy
	fetchedAt time.Time
}

// Get is the policy of src's run.
func (p *Policies) Get(ctx context.Context, src *Source) (*Policy, error) {
	if src.Run == "" {
		return nil, ErrNoRun
	}
	p.mu.Lock()
	if p.entries == nil {
		p.entries = map[string]*policyEntry{}
	}
	key := src.UID
	if key == "" {
		key = src.Run
	}
	entry := p.entries[key]
	if entry == nil {
		entry = &policyEntry{}
		p.entries[key] = entry
	}
	p.mu.Unlock()

	entry.mu.Lock()
	defer entry.mu.Unlock()
	if entry.policy != nil && time.Since(entry.fetchedAt) < p.TTL {
		return entry.policy, nil
	}
	policy, err := p.Source.Policy(ctx, src.Run)
	if err != nil {
		if entry.policy != nil && !errors.Is(err, ErrNoRun) {
			return entry.policy, nil
		}
		return nil, err
	}
	entry.policy, entry.fetchedAt = policy, time.Now()
	return policy, nil
}

// Forget drops the cached policies of pods that are no longer known.
func (p *Policies) Forget(keep func(uid string) bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	for key := range p.entries {
		if !keep(key) {
			delete(p.entries, key)
		}
	}
}

// UIDs are the pods the sources know.
func (s *Sources) UIDs() map[string]bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make(map[string]bool, len(s.byUID))
	for uid := range s.byUID {
		out[uid] = true
	}
	return out
}

// splitHostPort is host and port of host[:port], with def as the port when none is given.
func splitHostPort(hostport string, def int) (string, int) {
	host, port, err := net.SplitHostPort(hostport)
	if err != nil {
		return strings.Trim(hostport, "[]"), def
	}
	var n int
	if _, err := fmt.Sscanf(port, "%d", &n); err != nil {
		return host, def
	}
	return host, n
}
