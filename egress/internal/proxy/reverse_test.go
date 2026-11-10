package proxy

import (
	"bufio"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/http/cgi"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestModelRouteSwapsThePlaceholderForTheCredential(t *testing.T) {
	var mu sync.Mutex
	var seen http.Header
	var path string
	release := make(chan struct{})
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		seen, path = r.Header.Clone(), r.URL.RequestURI()
		mu.Unlock()
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, "event: message_start\n\n")
		w.(http.Flusher).Flush()
		<-release // the client must see the first event before the stream ends
		_, _ = io.WriteString(w, "event: message_stop\n\n")
	}))
	defer upstream.Close()
	h := newHarness(t, PhaseAgent, &Policy{Model: &Route{URL: upstream.URL, Headers: map[string]string{"x-api-key": "sk-real"}}})

	req, _ := http.NewRequest(http.MethodPost, h.reverse.URL+"/anthropic/v1/messages?beta=true", strings.NewReader(`{"model":"m"}`))
	req.Header.Set("x-api-key", "placeholder")
	req.Header.Set("Authorization", "Bearer placeholder")
	req.Header.Set("anthropic-version", "2023-06-01")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	reader := bufio.NewReader(resp.Body)
	first, err := reader.ReadString('\n')
	if err != nil || first != "event: message_start\n" {
		t.Fatalf("first line %q, %v", first, err)
	}
	close(release)
	rest, _ := io.ReadAll(reader)
	resp.Body.Close()
	if !strings.Contains(string(rest), "message_stop") {
		t.Fatalf("rest %q", rest)
	}
	mu.Lock()
	defer mu.Unlock()
	if seen.Get("X-Api-Key") != "sk-real" || seen.Get("Authorization") != "" || seen.Get("Anthropic-Version") != "2023-06-01" ||
		path != "/v1/messages?beta=true" {
		t.Fatalf("upstream saw %v at %s", seen, path)
	}
	records := h.records(t)
	if len(records) != 1 || records[0].Route != "model" || records[0].Path != "/v1/messages" || records[0].Status != 200 ||
		records[0].BytesSent != int64(len(`{"model":"m"}`)) || records[0].BytesReceived == 0 {
		t.Fatalf("records %+v", records)
	}
}

func TestModelRouteIsNotForSetup(t *testing.T) {
	h := newHarness(t, PhaseSetup, &Policy{Model: &Route{URL: "https://api.anthropic.com", Headers: map[string]string{"x-api-key": "k"}}})
	resp, err := http.Post(h.reverse.URL+"/anthropic/v1/messages", "application/json", strings.NewReader("{}"))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != 403 {
		t.Fatalf("status %d", resp.StatusCode)
	}
	h.policies.policy = &Policy{}
	h.setPhase(PhaseAgent)
	h.proxy.Policies = &Policies{Source: h.policies, TTL: time.Minute}
	resp, _ = http.Post(h.reverse.URL+"/anthropic/v1/messages", "application/json", strings.NewReader("{}"))
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 403 || !strings.Contains(string(body), "no model provider") {
		t.Fatalf("a run without a provider: %d %q", resp.StatusCode, body)
	}
}

func TestMCPRouteAddsTheSecretHeaders(t *testing.T) {
	var seen *http.Request
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = r.Clone(r.Context())
		_, _ = io.WriteString(w, `{"jsonrpc":"2.0","id":1,"result":{}}`)
	}))
	defer upstream.Close()
	h := newHarness(t, PhaseAgent, &Policy{Connections: []Connection{
		{Name: "tracker", URL: upstream.URL + "/mcp?team=a", Headers: map[string]string{"Authorization": "Bearer tracker-secret"}},
	}})
	req, _ := http.NewRequest(http.MethodPost, h.reverse.URL+"/mcp/tracker?x=1", strings.NewReader(`{}`))
	req.Header.Set("Mcp-Session-Id", "s1")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != 200 || seen.URL.Path != "/mcp" || seen.URL.RawQuery != "team=a&x=1" ||
		seen.Header.Get("Authorization") != "Bearer tracker-secret" || seen.Header.Get("Mcp-Session-Id") != "s1" {
		t.Fatalf("status %d, upstream saw %s %v", resp.StatusCode, seen.URL, seen.Header)
	}
	resp, _ = http.Post(h.reverse.URL+"/mcp/other", "application/json", strings.NewReader("{}"))
	resp.Body.Close()
	if resp.StatusCode != 403 {
		t.Fatalf("an unknown connection: %d", resp.StatusCode)
	}
}

func TestPhaseEndpoint(t *testing.T) {
	h := newHarness(t, PhaseSetup, &Policy{})
	read := func() map[string]any {
		resp, err := http.Get(h.reverse.URL + "/phase")
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		var out map[string]any
		_ = json.NewDecoder(resp.Body).Decode(&out)
		return out
	}
	if got := read(); got["phase"] != "setup" || got["run"] != "run-1" {
		t.Fatalf("%v", got)
	}
	h.setPhase(PhaseAgent)
	if got := read(); got["phase"] != "agent" {
		t.Fatalf("%v", got)
	}
}

// -- git through the proxy, against git http-backend -------------------------------------------

func gitCmd(t *testing.T, dir string, args ...string) (string, error) {
	t.Helper()
	cmd := exec.Command("git", args...)
	cmd.Dir = dir
	cmd.Env = append(cleanEnv(), "HOME="+dir, "GIT_TERMINAL_PROMPT=0", "GIT_CONFIG_NOSYSTEM=1",
		"GIT_AUTHOR_NAME=a", "GIT_AUTHOR_EMAIL=a@b.c", "GIT_COMMITTER_NAME=a", "GIT_COMMITTER_EMAIL=a@b.c")
	out, err := cmd.CombinedOutput()
	return string(out), err
}

func cleanEnv() []string {
	var env []string
	for _, kv := range os.Environ() {
		name := strings.ToUpper(strings.SplitN(kv, "=", 2)[0])
		if strings.HasSuffix(name, "_PROXY") || strings.HasPrefix(name, "GIT_") || name == "HOME" {
			continue
		}
		env = append(env, kv)
	}
	return env
}

func mustGit(t *testing.T, dir string, args ...string) string {
	t.Helper()
	out, err := gitCmd(t, dir, args...)
	if err != nil {
		t.Fatalf("git %v: %v\n%s", args, err, out)
	}
	return out
}

// gitServer serves bare repositories under root with git http-backend, behind Basic auth.
func gitServer(t *testing.T, root, credentials string) (*httptest.Server, *[]string) {
	t.Helper()
	execPath := strings.TrimSpace(mustGit(t, root, "--exec-path"))
	backend := &cgi.Handler{Path: filepath.Join(execPath, "git-http-backend"),
		Env: []string{"GIT_PROJECT_ROOT=" + root, "GIT_HTTP_EXPORT_ALL=1"}}
	var mu sync.Mutex
	var seen []string
	want := "Basic " + base64.StdEncoding.EncodeToString([]byte(credentials))
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		seen = append(seen, r.Method+" "+r.URL.Path+" auth="+map[bool]string{true: "ok", false: "no"}[r.Header.Get("Authorization") == want])
		mu.Unlock()
		if r.Header.Get("Authorization") != want {
			w.Header().Set("WWW-Authenticate", `Basic realm="git"`)
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		backend.ServeHTTP(w, r)
	}))
	t.Cleanup(server.Close)
	return server, &seen
}

func TestGitThroughTheProxy(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("no git")
	}
	root := t.TempDir()
	work := t.TempDir()
	for _, name := range []string{"rw.git", "ro.git"} {
		mustGit(t, root, "init", "--bare", "-b", "main", name)
		mustGit(t, filepath.Join(root, name), "config", "http.receivepack", "true")
	}
	seed := filepath.Join(work, "seed")
	mustGit(t, work, "init", "-b", "main", "seed")
	if err := os.WriteFile(filepath.Join(seed, "README"), []byte("hi\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	mustGit(t, seed, "add", ".")
	mustGit(t, seed, "commit", "-m", "first")
	mustGit(t, seed, "push", filepath.Join(root, "rw.git"), "main")
	mustGit(t, seed, "push", filepath.Join(root, "ro.git"), "main")

	server, seen := gitServer(t, root, "x-access-token:repo-token")
	host := strings.TrimPrefix(server.URL, "http://")
	auth := map[string]string{"Authorization": "Basic " + base64.StdEncoding.EncodeToString([]byte("x-access-token:repo-token"))}
	h := newHarness(t, PhaseSetup, &Policy{Repositories: []Repository{
		{URL: server.URL + "/rw.git", Push: true, Headers: auth},
		{URL: server.URL + "/ro.git", Push: false, Headers: auth},
	}})
	insteadOf := func(name string) []string {
		return []string{"-c", "url." + h.reverse.URL + "/git/" + host + "/" + name + ".insteadOf=" + server.URL + "/" + name}
	}

	// Clone in the setup phase; the remote stays the plain URL.
	mustGit(t, work, append(insteadOf("rw.git"), "clone", server.URL+"/rw.git", "rw")...)
	mustGit(t, work, append(insteadOf("ro.git"), "clone", server.URL+"/ro.git", "ro")...)
	if got := strings.TrimSpace(mustGit(t, filepath.Join(work, "rw"), "remote", "get-url", "origin")); got != server.URL+"/rw.git" {
		t.Fatalf("origin %q", got)
	}

	// Push in the agent phase: allowed for push: true, refused with git's own message for push: false.
	h.setPhase(PhaseAgent)
	for _, name := range []string{"rw", "ro"} {
		dir := filepath.Join(work, name)
		mustGit(t, dir, "checkout", "-b", "T-1-change")
		if err := os.WriteFile(filepath.Join(dir, "change"), []byte(name), 0o644); err != nil {
			t.Fatal(err)
		}
		mustGit(t, dir, "add", ".")
		mustGit(t, dir, "commit", "-m", "change")
	}
	mustGit(t, filepath.Join(work, "rw"), append(insteadOf("rw.git"), "push", "origin", "T-1-change")...)
	if out := mustGit(t, root, "--git-dir", filepath.Join(root, "rw.git"), "branch", "--list", "T-1-change"); !strings.Contains(out, "T-1-change") {
		t.Fatalf("the pushed branch is not there: %q", out)
	}
	out, err := gitCmd(t, filepath.Join(work, "ro"), append(insteadOf("ro.git"), "push", "origin", "T-1-change")...)
	if err == nil || !strings.Contains(out, "remote error: pushing to "+server.URL+"/ro.git is not allowed for this task (push: false)") {
		t.Fatalf("push to a push: false repository: %v\n%s", err, out)
	}
	if out := mustGit(t, root, "--git-dir", filepath.Join(root, "ro.git"), "branch", "--list", "T-1-change"); strings.TrimSpace(out) != "" {
		t.Fatal("the refused push reached the repository")
	}
	// A fetch from the read-only repository still works.
	mustGit(t, filepath.Join(work, "ro"), append(insteadOf("ro.git"), "fetch", "origin")...)

	for _, line := range *seen {
		if strings.HasSuffix(line, "auth=no") {
			t.Fatalf("a request reached the git server without the credential: %v", *seen)
		}
	}
	var refused []Record
	for _, r := range h.records(t) {
		if !r.Allowed {
			refused = append(refused, r)
		}
	}
	if len(refused) != 1 || refused[0].Route != "git" || !strings.Contains(refused[0].Reason, "push: false") {
		t.Fatalf("refused records %+v", refused)
	}
}

func TestGitRouteRefusesOtherPathsAndRepositories(t *testing.T) {
	h := newHarness(t, PhaseAgent, &Policy{Repositories: []Repository{{URL: "https://github.com/acme/app.git", Push: true}}})
	for path, want := range map[string]string{
		"/git/github.com/acme/app.git/settings":       "only git's own requests",
		"/git/github.com/acme/other.git/info/refs":    "not a repository of this run",
		"/git/github.com/acme/app.git-evil/info/refs": "not a repository of this run",
	} {
		resp, err := http.Get(h.reverse.URL + path)
		if err != nil {
			t.Fatal(err)
		}
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		if resp.StatusCode != 403 || !strings.Contains(string(body), want) {
			t.Errorf("%s: %d %q", path, resp.StatusCode, body)
		}
	}
}

func TestARefusedRepositoryCredentialNeverPromptsGit(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("no git")
	}
	root := t.TempDir()
	mustGit(t, root, "init", "--bare", "-b", "main", "app.git")
	server, _ := gitServer(t, root, "x-access-token:right")
	host := strings.TrimPrefix(server.URL, "http://")
	h := newHarness(t, PhaseSetup, &Policy{Repositories: []Repository{{URL: server.URL + "/app.git",
		Headers: map[string]string{"Authorization": "Basic " + base64.StdEncoding.EncodeToString([]byte("x-access-token:wrong"))}}}})
	out, err := gitCmd(t, t.TempDir(), "-c", "url."+h.reverse.URL+"/git/"+host+"/app.git.insteadOf="+server.URL+"/app.git",
		"ls-remote", server.URL+"/app.git")
	if err == nil || !strings.Contains(out, "403") || strings.Contains(out, "Username") {
		t.Fatalf("%v\n%s", err, out)
	}
}
