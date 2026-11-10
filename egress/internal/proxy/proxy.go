package proxy

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httputil"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// Proxy holds what both listeners share.
type Proxy struct {
	Sources  *Sources
	Policies *Policies
	Dialer   *Dialer
	Audit    *Auditor
	Log      *slog.Logger
	// LookupWait is how long a request from an address the pod watch does not know yet
	// waits for it (a pod that has just started).
	LookupWait time.Duration

	once      sync.Once
	forwardRT *http.Transport
	routeRT   *http.Transport
}

func (p *Proxy) init() {
	p.once.Do(func() {
		if p.Log == nil {
			p.Log = slog.New(slog.DiscardHandler)
		}
		if p.LookupWait == 0 {
			p.LookupWait = 10 * time.Second
		}
		p.forwardRT = p.Dialer.ForwardTransport()
		p.routeRT = p.Dialer.RouteTransport()
	})
}

func remoteIP(r *http.Request) string {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	return host
}

func (p *Proxy) source(r *http.Request, wait time.Duration) *Source {
	return p.Sources.Lookup(r.Context(), remoteIP(r), wait)
}

func (p *Proxy) record(rec Record) {
	if rec.Allowed {
		p.Log.Debug("egress", "run", rec.Run, "phase", rec.Phase, "route", rec.Route, "method", rec.Method,
			"host", rec.Host, "port", rec.Port, "path", rec.Path, "status", rec.Status)
	} else {
		p.Log.Info("refused", "run", rec.Run, "phase", rec.Phase, "route", rec.Route, "method", rec.Method,
			"host", rec.Host, "port", rec.Port, "path", rec.Path, "reason", rec.Reason)
	}
	if p.Audit != nil && rec.Run != "" {
		p.Audit.Add(rec)
	}
}

// decide is the forward proxy's rule: any host in the setup phase and for builds, the
// allowlist in the agent phase.
func (p *Proxy) decide(ctx context.Context, src *Source, host string) (bool, string) {
	if src.Phase.Open() {
		return true, ""
	}
	policy, err := p.Policies.Get(ctx, src)
	if err != nil {
		return false, policyProblem(err)
	}
	if policy.Allows(host) {
		return true, ""
	}
	return false, fmt.Sprintf("%s is not on this task's network allowlist (network.allow)", host)
}

func policyProblem(err error) string {
	if errors.Is(err, ErrNoRun) {
		return "the run of this pod is not active"
	}
	return "the network policy of this run could not be read"
}

func unknownSource(ip string) string {
	return fmt.Sprintf("requests from %s come from no run or build pod", ip)
}

// oneLine keeps a reason fit for a status line.
func oneLine(text string) string {
	text = strings.Map(func(r rune) rune {
		if r < 0x20 || r > 0x7e {
			return ' '
		}
		return r
	}, text)
	if len(text) > 200 {
		text = text[:200]
	}
	return text
}

// -- the forward proxy (:3128) ----------------------------------------------------------------

// Forward is the forward proxy: CONNECT tunnels decided by host name and port, and plain
// HTTP requests in absolute form.
func (p *Proxy) Forward() http.Handler {
	p.init()
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodConnect {
			p.connect(w, r)
			return
		}
		if !r.URL.IsAbs() || r.URL.Host == "" {
			http.Error(w, "this is a forward proxy: send CONNECT or an absolute URL", http.StatusBadRequest)
			return
		}
		p.plain(w, r)
	})
}

func (p *Proxy) connect(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	host, port := splitHostPort(r.Host, 443)
	rec := Record{Route: "connect", Host: host, Port: port, At: start.UTC()}
	src := p.source(r, p.LookupWait)
	if src == nil {
		rec.Reason = unknownSource(remoteIP(r))
		p.record(rec)
		refuseConnect(w, rec.Reason)
		return
	}
	rec.Run, rec.Phase = src.Run, src.Phase
	if ok, reason := p.decide(r.Context(), src, host); !ok {
		rec.Reason = reason
		p.record(rec)
		refuseConnect(w, reason)
		return
	}
	upstream, err := p.Dialer.Dial(r.Context(), host, port)
	if err != nil {
		if reason, ok := IsRefusal(err); ok {
			rec.Reason = reason
			p.record(rec)
			refuseConnect(w, reason)
			return
		}
		rec.Allowed, rec.Status, rec.Reason = true, http.StatusBadGateway, oneLine(err.Error())
		rec.DurationMs = time.Since(start).Milliseconds()
		p.record(rec)
		writeRaw(w, http.StatusBadGateway, "cannot reach "+host+": "+oneLine(err.Error()))
		return
	}
	defer upstream.Close()
	client, buffered, err := http.NewResponseController(w).Hijack()
	if err != nil {
		http.Error(w, "cannot tunnel on this connection", http.StatusInternalServerError)
		return
	}
	defer client.Close()
	rec.Allowed, rec.Status = true, http.StatusOK
	if _, err := client.Write([]byte("HTTP/1.1 200 Connection established\r\n\r\n")); err != nil {
		return
	}
	var sent, received atomic.Int64
	if n := buffered.Reader.Buffered(); n > 0 {
		head, _ := buffered.Reader.Peek(n)
		written, _ := upstream.Write(head)
		sent.Add(int64(written))
	}
	splice(client, upstream, &sent, &received)
	rec.BytesSent, rec.BytesReceived = sent.Load(), received.Load()
	rec.DurationMs = time.Since(start).Milliseconds()
	p.record(rec)
}

// splice copies both ways until both sides are done; a side that ends closes the other's
// write half.
func splice(client, upstream net.Conn, sent, received *atomic.Int64) {
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		n, _ := io.Copy(upstream, client)
		sent.Add(n)
		closeWrite(upstream)
	}()
	go func() {
		defer wg.Done()
		n, _ := io.Copy(client, upstream)
		received.Add(n)
		closeWrite(client)
	}()
	wg.Wait()
}

func closeWrite(conn net.Conn) {
	type writeCloser interface{ CloseWrite() error }
	if c, ok := conn.(writeCloser); ok {
		_ = c.CloseWrite()
		return
	}
	if b, ok := conn.(*bufferedConn); ok {
		closeWrite(b.Conn)
		return
	}
	_ = conn.Close()
}

// refuseConnect answers 403 with the reason as the status line's text, which clients
// that show nothing else (Python, Java) still print, and as the body.
func refuseConnect(w http.ResponseWriter, reason string) {
	writeRaw(w, http.StatusForbidden, reason)
}

func writeRaw(w http.ResponseWriter, code int, reason string) {
	reason = oneLine(reason)
	conn, _, err := http.NewResponseController(w).Hijack()
	if err != nil {
		http.Error(w, reason, code)
		return
	}
	defer conn.Close()
	body := reason + "\n"
	fmt.Fprintf(conn, "HTTP/1.1 %d %s\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s",
		code, reason, len(body), body)
}

func (p *Proxy) plain(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	host := r.URL.Hostname()
	port := 80
	if r.URL.Port() != "" {
		_, port = splitHostPort(r.URL.Host, 80)
	} else if r.URL.Scheme == "https" {
		port = 443
	}
	rec := Record{Route: "http", Method: r.Method, Host: host, Port: port, Path: r.URL.Path, At: start.UTC()}
	src := p.source(r, p.LookupWait)
	if src == nil {
		rec.Reason = unknownSource(remoteIP(r))
		p.record(rec)
		http.Error(w, rec.Reason, http.StatusForbidden)
		return
	}
	rec.Run, rec.Phase = src.Run, src.Phase
	if ok, reason := p.decide(r.Context(), src, host); !ok {
		rec.Reason = reason
		p.record(rec)
		http.Error(w, reason, http.StatusForbidden)
		return
	}
	if err := p.Dialer.Check(r.Context(), host, port); err != nil {
		if reason, ok := IsRefusal(err); ok {
			rec.Reason = reason
			p.record(rec)
			http.Error(w, reason, http.StatusForbidden)
			return
		}
	}
	rec.Allowed = true
	target := *r.URL
	p.serve(w, r, &rec, start, &httputil.ReverseProxy{
		Rewrite: func(pr *httputil.ProxyRequest) {
			pr.Out.URL = &target
			pr.Out.Host = target.Host
		},
		Transport: p.forwardRT,
	})
}

// -- serving through a ReverseProxy, counted --------------------------------------------------

type countingBody struct {
	io.ReadCloser
	n *atomic.Int64
}

func (b countingBody) Read(p []byte) (int, error) {
	n, err := b.ReadCloser.Read(p)
	b.n.Add(int64(n))
	return n, err
}

type countingWriter struct {
	http.ResponseWriter
	status int
	n      int64
}

func (w *countingWriter) WriteHeader(code int) {
	if w.status == 0 {
		w.status = code
	}
	w.ResponseWriter.WriteHeader(code)
}

func (w *countingWriter) Write(b []byte) (int, error) {
	if w.status == 0 {
		w.status = http.StatusOK
	}
	n, err := w.ResponseWriter.Write(b)
	w.n += int64(n)
	return n, err
}

func (w *countingWriter) Unwrap() http.ResponseWriter { return w.ResponseWriter }

// serve runs rp for the request and completes rec with what happened.
func (p *Proxy) serve(w http.ResponseWriter, r *http.Request, rec *Record, start time.Time, rp *httputil.ReverseProxy) {
	var sent atomic.Int64
	if r.Body != nil && r.Body != http.NoBody {
		r.Body = countingBody{r.Body, &sent}
	}
	cw := &countingWriter{ResponseWriter: w}
	rp.FlushInterval = -1
	rp.ErrorLog = slog.NewLogLogger(p.Log.Handler(), slog.LevelDebug)
	rp.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		if reason, ok := IsRefusal(err); ok {
			rec.Allowed, rec.Reason = false, reason
			http.Error(w, reason, http.StatusForbidden)
			return
		}
		if errors.Is(err, context.Canceled) {
			rec.Reason = "the client went away"
			return
		}
		rec.Reason = oneLine(err.Error())
		http.Error(w, "cannot reach "+rec.Host+": "+rec.Reason, http.StatusBadGateway)
	}
	rp.ServeHTTP(cw, r)
	rec.Status = cw.status
	rec.BytesSent, rec.BytesReceived = sent.Load(), cw.n
	rec.DurationMs = time.Since(start).Milliseconds()
	p.record(*rec)
}
