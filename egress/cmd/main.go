// exradius-egress is the egress proxy of run and build pods (docs/kubernetes.md §11).
//
// Two listeners: the forward proxy (HTTP_PROXY, HTTPS_PROXY), which decides on CONNECT by
// host name, and the injecting reverse proxy, whose routes (/anthropic, /git/<host>/<path>,
// /mcp/<connection>) forward to the run's model, repositories and HTTP connections with
// the real credentials. A request's run is its source address, from a watch of the pods
// in the runs and builds namespaces. Credentials and allowlists come from the api's
// internal port and live in memory only; every request is recorded there in batches.
package main

import (
	"context"
	"crypto/x509"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/musabeytekin/exradius/egress/internal/kube"
	"github.com/musabeytekin/exradius/egress/internal/proxy"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "exradius-egress:", err)
		os.Exit(1)
	}
}

func split(list string) []string {
	var out []string
	for _, item := range strings.Split(list, ",") {
		if item = strings.TrimSpace(item); item != "" {
			out = append(out, item)
		}
	}
	return out
}

func run() error {
	var (
		forwardAddr  = flag.String("forward-addr", ":3128", "the forward proxy's address")
		reverseAddr  = flag.String("reverse-addr", ":3129", "the reverse proxy's address")
		healthAddr   = flag.String("health-addr", ":8080", "/healthz and /readyz")
		apiURL       = flag.String("api-url", "", "the api's internal port, http://exradius-api-internal.<ns>.svc:8001")
		tokenFile    = flag.String("token-file", "/var/run/secrets/exradius-api/token", "the ServiceAccount token for the api (audience exradius-api)")
		runsNS       = flag.String("runs-namespace", "exradius-runs", "where run pods are")
		buildsNS     = flag.String("builds-namespace", "exradius-builds", "where build pods are; empty: builds do not use the proxy")
		upstream     = flag.String("upstream-proxy", os.Getenv("EXRADIUS_UPSTREAM_PROXY"), "a corporate proxy every outbound connection goes through (http://[user:password@]host:port)")
		noUpstream   = flag.String("upstream-no-proxy", "", "comma-separated hosts (or *.domain) reached without the upstream proxy")
		allowPrivate = flag.String("allow-private", "", "comma-separated private destinations the forward proxy may reach: CIDRs or host[:port]")
		caBundle     = flag.String("ca-bundle", "", "PEM file of extra certificate authorities (an intercepting upstream proxy's)")
		policyTTL    = flag.Duration("policy-ttl", 5*time.Minute, "how long a run's policy is used before it is read again")
		logLevel     = flag.String("log-level", "info", "debug logs every request; info only refusals and errors")
	)
	flag.Parse()

	level := slog.LevelInfo
	if *logLevel == "debug" {
		level = slog.LevelDebug
	}
	log := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: level}))
	if *apiURL == "" {
		return errors.New("--api-url is required")
	}

	roots, err := x509.SystemCertPool()
	if err != nil || roots == nil {
		roots = x509.NewCertPool()
	}
	if *caBundle != "" {
		pem, err := os.ReadFile(*caBundle)
		if err != nil {
			return fmt.Errorf("--ca-bundle: %w", err)
		}
		if !roots.AppendCertsFromPEM(pem) {
			return fmt.Errorf("--ca-bundle: no certificate in %s", *caBundle)
		}
	}
	dialer := &proxy.Dialer{NoUpstream: split(*noUpstream), RootCAs: roots}
	if *upstream != "" {
		u, err := url.Parse(*upstream)
		if err != nil || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") {
			return fmt.Errorf("--upstream-proxy: not an http(s) URL")
		}
		dialer.Upstream = u
	}
	if dialer.AllowPrivate, err = proxy.ParsePrivateAllow(*allowPrivate); err != nil {
		return err
	}

	namespaces := []string{*runsNS}
	if *buildsNS != "" {
		namespaces = append(namespaces, *buildsNS)
	}
	sources := proxy.NewSources(namespaces...)
	api := &proxy.API{Base: *apiURL, TokenFile: *tokenFile}
	policies := &proxy.Policies{Source: api, TTL: *policyTTL}
	auditor := &proxy.Auditor{Sink: api, Log: log}
	p := &proxy.Proxy{Sources: sources, Policies: policies, Dialer: dialer, Audit: auditor, Log: log}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	client, err := kube.InCluster()
	if err != nil {
		return err
	}
	go client.WatchPods(ctx, *runsNS, proxy.LabelRun, sources, log)
	if *buildsNS != "" {
		go client.WatchPods(ctx, *buildsNS, proxy.LabelPurpose+"="+proxy.PurposeBuild, sources, log)
	}
	go auditor.Run(ctx)
	go func() {
		ticker := time.NewTicker(time.Minute)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				known := sources.UIDs()
				policies.Forget(func(uid string) bool { return known[uid] })
			}
		}
	}()

	health := http.NewServeMux()
	health.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte("ok\n")) })
	health.HandleFunc("/readyz", func(w http.ResponseWriter, r *http.Request) {
		if !sources.Ready() {
			http.Error(w, "the pod watch has not listed every namespace yet", http.StatusServiceUnavailable)
			return
		}
		_, _ = w.Write([]byte("ok\n"))
	})

	servers := []*http.Server{
		{Addr: *forwardAddr, Handler: p.Forward(), ReadHeaderTimeout: 30 * time.Second},
		{Addr: *reverseAddr, Handler: p.Reverse(), ReadHeaderTimeout: 30 * time.Second},
		{Addr: *healthAddr, Handler: health, ReadHeaderTimeout: 10 * time.Second},
	}
	errs := make(chan error, len(servers))
	for _, server := range servers {
		listener, err := net.Listen("tcp", server.Addr)
		if err != nil {
			return err
		}
		go func() { errs <- server.Serve(listener) }()
	}
	log.Info("exradius-egress is running", "forward", *forwardAddr, "reverse", *reverseAddr,
		"namespaces", namespaces, "upstream", dialer.Upstream != nil)

	select {
	case <-ctx.Done():
	case err := <-errs:
		if !errors.Is(err, http.ErrServerClosed) {
			log.Error("a listener failed", "error", err)
		}
	}
	shutdown, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	for _, server := range servers {
		_ = server.Shutdown(shutdown)
	}
	auditor.Flush(shutdown)
	return nil
}
