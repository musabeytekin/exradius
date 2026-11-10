package proxy

import (
	"bufio"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"strconv"
	"strings"
	"time"
)

// Refusal is a request the proxy will not forward, with the one line the client sees.
type Refusal struct{ Reason string }

func (r *Refusal) Error() string { return r.Reason }

// internalRanges are addresses the forward proxy never reaches unless the install allows
// them (egress.allowPrivate): the cluster's pods, services and nodes live there, and so
// does a cloud's metadata endpoint.
var internalRanges = []netip.Prefix{
	netip.MustParsePrefix("0.0.0.0/8"),
	netip.MustParsePrefix("10.0.0.0/8"),
	netip.MustParsePrefix("100.64.0.0/10"),
	netip.MustParsePrefix("127.0.0.0/8"),
	netip.MustParsePrefix("169.254.0.0/16"),
	netip.MustParsePrefix("172.16.0.0/12"),
	netip.MustParsePrefix("192.168.0.0/16"),
	netip.MustParsePrefix("224.0.0.0/4"),
	netip.MustParsePrefix("240.0.0.0/4"),
	netip.MustParsePrefix("::/128"),
	netip.MustParsePrefix("::1/128"),
	netip.MustParsePrefix("fc00::/7"),
	netip.MustParsePrefix("fe80::/10"),
	netip.MustParsePrefix("ff00::/8"),
}

// Internal reports whether ip is a private, loopback, link-local or otherwise
// non-public address.
func Internal(ip netip.Addr) bool {
	ip = ip.Unmap()
	for _, prefix := range internalRanges {
		if prefix.Contains(ip) {
			return true
		}
	}
	return false
}

// PrivateAllow is one egress.allowPrivate entry: a CIDR, or host[:port] (no port: any).
type PrivateAllow struct {
	Prefix netip.Prefix
	Host   string
	Port   int
}

// ParsePrivateAllow reads a comma-separated egress.allowPrivate list.
func ParsePrivateAllow(list string) ([]PrivateAllow, error) {
	var out []PrivateAllow
	for _, item := range strings.Split(list, ",") {
		item = strings.TrimSpace(item)
		if item == "" {
			continue
		}
		if prefix, err := netip.ParsePrefix(item); err == nil {
			out = append(out, PrivateAllow{Prefix: prefix.Masked()})
			continue
		}
		if addr, err := netip.ParseAddr(item); err == nil {
			out = append(out, PrivateAllow{Prefix: netip.PrefixFrom(addr, addr.BitLen())})
			continue
		}
		host, port := item, 0
		if h, p, err := net.SplitHostPort(item); err == nil {
			n, err := strconv.Atoi(p)
			if err != nil || n < 1 || n > 65535 {
				return nil, fmt.Errorf("allowPrivate %q: bad port", item)
			}
			host, port = h, n
		}
		if host == "" {
			return nil, fmt.Errorf("allowPrivate %q: no host", item)
		}
		out = append(out, PrivateAllow{Host: strings.ToLower(host), Port: port})
	}
	return out, nil
}

func (a PrivateAllow) allows(host string, ip netip.Addr, port int) bool {
	if a.Prefix.IsValid() {
		return a.Prefix.Contains(ip.Unmap())
	}
	return strings.EqualFold(a.Host, host) && (a.Port == 0 || a.Port == port)
}

// Dialer opens the forward proxy's upstream connections: straight to the destination, or
// through the install's upstream proxy (egress.upstreamProxy), never to an internal
// address the install did not allow.
type Dialer struct {
	Upstream     *url.URL
	NoUpstream   []string
	AllowPrivate []PrivateAllow
	Resolver     *net.Resolver
	Timeout      time.Duration
	// RootCAs verifies TLS to an https upstream proxy and the reverse routes' upstreams.
	RootCAs *x509.CertPool
}

func (d *Dialer) resolver() *net.Resolver {
	if d.Resolver != nil {
		return d.Resolver
	}
	return net.DefaultResolver
}

func (d *Dialer) timeout() time.Duration {
	if d.Timeout > 0 {
		return d.Timeout
	}
	return 30 * time.Second
}

// viaUpstream reports whether connections to host go through the upstream proxy.
func (d *Dialer) viaUpstream(host string) bool {
	return d.Upstream != nil && !MatchHost(d.NoUpstream, host) && !matchExact(d.NoUpstream, host)
}

func matchExact(list []string, host string) bool {
	for _, item := range list {
		if strings.EqualFold(strings.TrimSpace(item), host) {
			return true
		}
	}
	return false
}

func (d *Dialer) allowedPrivate(host string, ip netip.Addr, port int) bool {
	for _, a := range d.AllowPrivate {
		if a.allows(host, ip, port) {
			return true
		}
	}
	return false
}

// addresses resolves host and keeps the addresses the proxy may reach. Through an
// upstream proxy a name that does not resolve here is the upstream's to resolve.
func (d *Dialer) addresses(ctx context.Context, host string, port int) ([]netip.Addr, error) {
	if ip, err := netip.ParseAddr(host); err == nil {
		if Internal(ip) && !d.allowedPrivate(host, ip, port) {
			return nil, &Refusal{fmt.Sprintf("%s is a private or cluster-internal address", host)}
		}
		return []netip.Addr{ip}, nil
	}
	lookup, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	found, err := d.resolver().LookupNetIP(lookup, "ip", host)
	if err != nil {
		if d.viaUpstream(host) {
			return nil, nil
		}
		return nil, fmt.Errorf("cannot resolve %s: %w", host, err)
	}
	var allowed []netip.Addr
	for _, ip := range found {
		if !Internal(ip) || d.allowedPrivate(host, ip, port) {
			allowed = append(allowed, ip.Unmap())
		}
	}
	if len(allowed) == 0 {
		return nil, &Refusal{fmt.Sprintf("%s resolves to a private or cluster-internal address", host)}
	}
	return allowed, nil
}

// Check refuses a destination the forward proxy may not reach.
func (d *Dialer) Check(ctx context.Context, host string, port int) error {
	_, err := d.addresses(ctx, host, port)
	return err
}

// Dial connects to host:port for a tunnel.
func (d *Dialer) Dial(ctx context.Context, host string, port int) (net.Conn, error) {
	addrs, err := d.addresses(ctx, host, port)
	if err != nil {
		return nil, err
	}
	if d.viaUpstream(host) {
		return d.dialUpstream(ctx, net.JoinHostPort(host, strconv.Itoa(port)))
	}
	dialer := net.Dialer{Timeout: d.timeout()}
	var last error
	for _, ip := range addrs {
		conn, err := dialer.DialContext(ctx, "tcp", netip.AddrPortFrom(ip, uint16(port)).String())
		if err == nil {
			return conn, nil
		}
		last = err
	}
	return nil, last
}

// dialUpstream opens a tunnel through the upstream proxy with CONNECT.
func (d *Dialer) dialUpstream(ctx context.Context, target string) (net.Conn, error) {
	address := d.Upstream.Host
	if d.Upstream.Port() == "" {
		port := "80"
		if d.Upstream.Scheme == "https" {
			port = "443"
		}
		address = net.JoinHostPort(d.Upstream.Hostname(), port)
	}
	dialer := net.Dialer{Timeout: d.timeout()}
	conn, err := dialer.DialContext(ctx, "tcp", address)
	if err != nil {
		return nil, fmt.Errorf("upstream proxy: %w", err)
	}
	if d.Upstream.Scheme == "https" {
		tlsConn := tls.Client(conn, &tls.Config{ServerName: d.Upstream.Hostname(), RootCAs: d.RootCAs, MinVersion: tls.VersionTLS12})
		if err := tlsConn.HandshakeContext(ctx); err != nil {
			conn.Close()
			return nil, fmt.Errorf("upstream proxy: %w", err)
		}
		conn = tlsConn
	}
	req := &http.Request{Method: http.MethodConnect, URL: &url.URL{Opaque: target}, Host: target, Header: http.Header{}}
	if auth := ProxyAuthorization(d.Upstream); auth != "" {
		req.Header.Set("Proxy-Authorization", auth)
	}
	if deadline, ok := ctx.Deadline(); ok {
		_ = conn.SetDeadline(deadline)
	} else {
		_ = conn.SetDeadline(time.Now().Add(d.timeout()))
	}
	if err := req.Write(conn); err != nil {
		conn.Close()
		return nil, fmt.Errorf("upstream proxy: %w", err)
	}
	reader := bufio.NewReader(conn)
	resp, err := http.ReadResponse(reader, req)
	if err != nil {
		conn.Close()
		return nil, fmt.Errorf("upstream proxy: %w", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		conn.Close()
		return nil, &Refusal{fmt.Sprintf("the upstream proxy refused %s: %s", target, resp.Status)}
	}
	_ = conn.SetDeadline(time.Time{})
	return &bufferedConn{Conn: conn, reader: reader}, nil
}

// ProxyAuthorization is the Basic header of a proxy URL's user info, or "".
func ProxyAuthorization(u *url.URL) string {
	if u == nil || u.User == nil {
		return ""
	}
	password, _ := u.User.Password()
	return "Basic " + base64.StdEncoding.EncodeToString([]byte(u.User.Username()+":"+password))
}

type bufferedConn struct {
	net.Conn
	reader *bufio.Reader
}

func (c *bufferedConn) Read(p []byte) (int, error) { return c.reader.Read(p) }

// ForwardTransport forwards the forward proxy's plain HTTP requests: through the upstream
// proxy when there is one, else straight to the checked destination.
func (d *Dialer) ForwardTransport() *http.Transport {
	t := &http.Transport{
		Proxy: func(req *http.Request) (*url.URL, error) {
			if d.viaUpstream(req.URL.Hostname()) {
				return d.Upstream, nil
			}
			return nil, nil
		},
		DialContext: func(ctx context.Context, network, address string) (net.Conn, error) {
			host, port := splitHostPort(address, 80)
			if d.Upstream != nil && strings.EqualFold(address, d.upstreamAddress()) {
				return (&net.Dialer{Timeout: d.timeout()}).DialContext(ctx, network, address)
			}
			return d.Dial(ctx, host, port)
		},
		TLSClientConfig:       &tls.Config{RootCAs: d.RootCAs, MinVersion: tls.VersionTLS12},
		MaxIdleConnsPerHost:   4,
		IdleConnTimeout:       60 * time.Second,
		ResponseHeaderTimeout: 5 * time.Minute,
	}
	return t
}

func (d *Dialer) upstreamAddress() string {
	if d.Upstream == nil {
		return ""
	}
	if d.Upstream.Port() != "" {
		return d.Upstream.Host
	}
	if d.Upstream.Scheme == "https" {
		return net.JoinHostPort(d.Upstream.Hostname(), "443")
	}
	return net.JoinHostPort(d.Upstream.Hostname(), "80")
}

// RouteTransport reaches the reverse routes' upstreams (the model, repositories and
// connections a config declares): through the upstream proxy when there is one. Their
// addresses are the config's, so the internal check does not apply.
func (d *Dialer) RouteTransport() *http.Transport {
	return &http.Transport{
		Proxy: func(req *http.Request) (*url.URL, error) {
			if d.viaUpstream(req.URL.Hostname()) {
				return d.Upstream, nil
			}
			return nil, nil
		},
		DialContext:           (&net.Dialer{Timeout: d.timeout(), KeepAlive: 30 * time.Second}).DialContext,
		TLSClientConfig:       &tls.Config{RootCAs: d.RootCAs, MinVersion: tls.VersionTLS12},
		ForceAttemptHTTP2:     true,
		MaxIdleConnsPerHost:   16,
		IdleConnTimeout:       90 * time.Second,
		TLSHandshakeTimeout:   15 * time.Second,
		ResponseHeaderTimeout: 10 * time.Minute,
	}
}

// IsRefusal reports whether err is a Refusal, and its reason.
func IsRefusal(err error) (string, bool) {
	var r *Refusal
	if errors.As(err, &r) {
		return r.Reason, true
	}
	return "", false
}
