package proxy

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strconv"
	"strings"
	"time"
)

// Route prefixes of the reverse proxy.
const (
	ModelPrefix = "/anthropic"
	GitPrefix   = "/git/"
	MCPPrefix   = "/mcp/"
	PhasePath   = "/phase"
)

// credentialHeaders are removed from every request on a reverse route before the route's
// own are set: a pod holds placeholders at most.
var credentialHeaders = []string{"Authorization", "X-Api-Key", "Proxy-Authorization"}

// Reverse is the injecting reverse proxy: the model, git and MCP routes, each forwarded
// over HTTPS with the run's real credential, and /phase, which tells a pod what the
// proxy sees of it.
func (p *Proxy) Reverse() http.Handler {
	p.init()
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == PhasePath:
			p.phase(w, r)
		case r.URL.Path == ModelPrefix || strings.HasPrefix(r.URL.Path, ModelPrefix+"/"):
			p.route(w, r, "model")
		case strings.HasPrefix(r.URL.Path, GitPrefix):
			p.route(w, r, "git")
		case strings.HasPrefix(r.URL.Path, MCPPrefix):
			p.route(w, r, "mcp")
		default:
			http.NotFound(w, r)
		}
	})
}

// phase answers {"run", "pod", "phase"} for the calling pod. The runner waits for "agent"
// before its session starts, so no agent request is ever decided on a stale setup phase.
func (p *Proxy) phase(w http.ResponseWriter, r *http.Request) {
	src := p.source(r, time.Second)
	w.Header().Set("Content-Type", "application/json")
	if src == nil {
		w.WriteHeader(http.StatusNotFound)
		_ = json.NewEncoder(w).Encode(map[string]any{"phase": nil, "error": unknownSource(remoteIP(r))})
		return
	}
	_ = json.NewEncoder(w).Encode(map[string]any{"run": src.Run, "pod": src.Pod, "phase": src.Phase})
}

// target is where a reverse route goes and what it adds.
type target struct {
	url     *url.URL
	headers map[string]string
	// gitPush is set for a git route that pushes (git-receive-pack).
	gitPush bool
}

func (p *Proxy) route(w http.ResponseWriter, r *http.Request, kind string) {
	start := time.Now()
	rec := Record{Route: kind, Method: r.Method, At: start.UTC()}
	src := p.source(r, p.LookupWait)
	if src == nil {
		rec.Reason = unknownSource(remoteIP(r))
		p.record(rec)
		http.Error(w, rec.Reason, http.StatusForbidden)
		return
	}
	rec.Run, rec.Phase = src.Run, src.Phase
	refuse := func(code int, reason string) {
		rec.Reason, rec.Status = reason, code
		p.record(rec)
		http.Error(w, reason, code)
	}
	if src.Build() {
		refuse(http.StatusForbidden, "build pods have no routes")
		return
	}
	policy, err := p.Policies.Get(r.Context(), src)
	if err != nil {
		refuse(http.StatusForbidden, policyProblem(err))
		return
	}
	var t *target
	var problem string
	switch kind {
	case "model":
		t, problem = modelTarget(policy, src, r)
	case "git":
		t, problem = gitTarget(policy, r)
	case "mcp":
		t, problem = mcpTarget(policy, src, r)
	}
	if t != nil {
		rec.Host, rec.Port = t.url.Hostname(), defaultPort(t.url)
		rec.Path = t.url.Path
	}
	if problem != "" {
		if t != nil && t.gitPush && isRefsAdvertisement(r) {
			// git prints an ERR packet of the advertisement as "remote error: <text>".
			rec.Reason, rec.Status = problem, http.StatusOK
			p.record(rec)
			writeGitError(w, problem)
			return
		}
		refuse(http.StatusForbidden, problem)
		return
	}
	rec.Allowed = true
	p.serve(w, r, &rec, start, &httputil.ReverseProxy{
		Rewrite: func(pr *httputil.ProxyRequest) {
			pr.Out.URL = t.url
			pr.Out.Host = t.url.Host
			for _, name := range credentialHeaders {
				pr.Out.Header.Del(name)
			}
			for name, value := range t.headers {
				pr.Out.Header.Set(name, value)
			}
		},
		Transport: p.routeRT,
		ModifyResponse: func(resp *http.Response) error {
			if kind == "git" && resp.StatusCode == http.StatusUnauthorized {
				// A refused credential: never ask git to prompt for one.
				resp.Header.Del("Www-Authenticate")
				message := fmt.Sprintf("%s refused the repository's credential\n", t.url.Host)
				resp.StatusCode, resp.Status = http.StatusForbidden, "403 Forbidden"
				resp.Body.Close()
				resp.Body = io.NopCloser(strings.NewReader(message))
				resp.ContentLength = int64(len(message))
				resp.Header.Set("Content-Length", strconv.Itoa(len(message)))
				resp.Header.Set("Content-Type", "text/plain; charset=utf-8")
			}
			return nil
		},
	})
}

func defaultPort(u *url.URL) int {
	if u.Port() != "" {
		n, _ := strconv.Atoi(u.Port())
		return n
	}
	if u.Scheme == "http" {
		return 80
	}
	return 443
}

// joined is base with the request's remaining path and query appended.
func joined(base string, rest string, query string) (*url.URL, error) {
	u, err := url.Parse(base)
	if err != nil {
		return nil, err
	}
	if rest != "" {
		u.Path = strings.TrimRight(u.Path, "/") + "/" + strings.TrimLeft(rest, "/")
		u.RawPath = ""
	}
	switch {
	case query == "":
	case u.RawQuery == "":
		u.RawQuery = query
	default:
		u.RawQuery += "&" + query
	}
	return u, nil
}

func modelTarget(policy *Policy, src *Source, r *http.Request) (*target, string) {
	if policy.Model == nil {
		return nil, "this run has no model provider"
	}
	u, err := joined(policy.Model.URL, strings.TrimPrefix(r.URL.Path, ModelPrefix), r.URL.RawQuery)
	if err != nil {
		return nil, "the model route is misconfigured"
	}
	t := &target{url: u, headers: policy.Model.Headers}
	if src.Phase != PhaseAgent {
		return t, "the model route is for the agent, not for setup"
	}
	return t, ""
}

func mcpTarget(policy *Policy, src *Source, r *http.Request) (*target, string) {
	rest := strings.TrimPrefix(r.URL.Path, MCPPrefix)
	name, sub, _ := strings.Cut(rest, "/")
	for _, conn := range policy.Connections {
		if conn.Name != name {
			continue
		}
		u, err := joined(conn.URL, sub, r.URL.RawQuery)
		if err != nil {
			return nil, "connection " + name + " has a bad URL"
		}
		t := &target{url: u, headers: conn.Headers}
		if src.Phase != PhaseAgent {
			return t, "connections are for the agent, not for setup"
		}
		return t, ""
	}
	return nil, fmt.Sprintf("%s is not an HTTP connection of this run", name)
}

// repoKey is a repository URL as its git route spells it: host and path, without the
// scheme, user info or a trailing slash.
func repoKey(raw string) (string, *url.URL, bool) {
	u, err := url.Parse(raw)
	if err != nil || u.Host == "" || (u.Scheme != "https" && u.Scheme != "http") {
		return "", nil, false
	}
	return u.Host + strings.TrimRight(u.Path, "/"), u, true
}

// gitPaths are the paths below a repository that git's HTTP transports use.
var gitPaths = []string{"/info/refs", "/git-upload-pack", "/git-receive-pack", "/HEAD"}
var gitPrefixes = []string{"/objects/", "/info/"}

func isGitPath(suffix string) bool {
	for _, p := range gitPaths {
		if suffix == p {
			return true
		}
	}
	for _, p := range gitPrefixes {
		if strings.HasPrefix(suffix, p) {
			return true
		}
	}
	return false
}

func isPush(r *http.Request, suffix string) bool {
	return suffix == "/git-receive-pack" || (suffix == "/info/refs" && r.URL.Query().Get("service") == "git-receive-pack")
}

func isRefsAdvertisement(r *http.Request) bool {
	return strings.HasSuffix(r.URL.Path, "/info/refs")
}

func gitTarget(policy *Policy, r *http.Request) (*target, string) {
	path := strings.TrimPrefix(r.URL.Path, GitPrefix)
	for _, repo := range policy.Repositories {
		key, u, ok := repoKey(repo.URL)
		if !ok || !(path == key || strings.HasPrefix(path, key+"/")) {
			continue
		}
		suffix := path[len(key):]
		out := *u
		out.User = nil
		out.Path = strings.TrimRight(u.Path, "/") + suffix
		out.RawPath = ""
		out.RawQuery = r.URL.RawQuery
		t := &target{url: &out, headers: repo.Headers, gitPush: isPush(r, suffix)}
		if !isGitPath(suffix) {
			return t, "only git's own requests go to a repository"
		}
		if t.gitPush && !repo.Push {
			return t, fmt.Sprintf("pushing to %s is not allowed for this task (push: false)", publicURL(u))
		}
		return t, ""
	}
	return nil, "not a repository of this run"
}

func publicURL(u *url.URL) string {
	out := *u
	out.User, out.RawQuery, out.Fragment = nil, "", ""
	return out.String()
}

// writeGitError answers a ref advertisement with an ERR packet, which git shows as
// "fatal: remote error: <message>".
func writeGitError(w http.ResponseWriter, message string) {
	w.Header().Set("Content-Type", "application/x-git-receive-pack-advertisement")
	w.Header().Set("Cache-Control", "no-cache")
	w.WriteHeader(http.StatusOK)
	_, _ = io.WriteString(w, pktLine("# service=git-receive-pack\n")+"0000"+pktLine("ERR "+oneLine(message)+"\n"))
}

func pktLine(payload string) string { return fmt.Sprintf("%04x%s", len(payload)+4, payload) }
