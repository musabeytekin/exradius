// Package kube is the egress proxy's view of the Kubernetes API: a list and watch of pods
// in the runs and builds namespaces, with nothing but the standard library. The proxy may
// do nothing else there (docs/kubernetes.md §3).
package kube

import (
	"bufio"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"
)

// In-cluster paths of the pod's own ServiceAccount.
const (
	ServiceAccountDir = "/var/run/secrets/kubernetes.io/serviceaccount"
)

// Client reaches the API server with a bearer token read from a file, which the kubelet
// rotates.
type Client struct {
	Base      string
	TokenFile string
	HTTP      *http.Client

	mu     sync.Mutex
	token  string
	readAt time.Time
}

// InCluster is the client of a pod's own ServiceAccount.
func InCluster() (*Client, error) {
	host, port := os.Getenv("KUBERNETES_SERVICE_HOST"), os.Getenv("KUBERNETES_SERVICE_PORT")
	if host == "" || port == "" {
		return nil, errors.New("not in a cluster: KUBERNETES_SERVICE_HOST is not set")
	}
	ca, err := os.ReadFile(ServiceAccountDir + "/ca.crt")
	if err != nil {
		return nil, err
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(ca) {
		return nil, errors.New("no certificate in the ServiceAccount's ca.crt")
	}
	transport := &http.Transport{
		TLSClientConfig: &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12},
		Proxy:           nil,
	}
	return &Client{
		Base:      "https://" + net.JoinHostPort(host, port),
		TokenFile: ServiceAccountDir + "/token",
		HTTP:      &http.Client{Transport: transport},
	}, nil
}

func (c *Client) bearer() (string, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.TokenFile == "" {
		return "", nil
	}
	if c.token == "" || time.Since(c.readAt) > time.Minute {
		data, err := os.ReadFile(c.TokenFile)
		if err != nil {
			return "", err
		}
		c.token, c.readAt = strings.TrimSpace(string(data)), time.Now()
	}
	return c.token, nil
}

func (c *Client) get(ctx context.Context, path string, query url.Values) (*http.Response, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.Base+path+"?"+query.Encode(), nil)
	if err != nil {
		return nil, err
	}
	token, err := c.bearer()
	if err != nil {
		return nil, err
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	req.Header.Set("Accept", "application/json")
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		resp.Body.Close()
		return nil, &StatusError{Code: resp.StatusCode, Body: strings.TrimSpace(string(body))}
	}
	return resp, nil
}

// StatusError is an API answer other than 200.
type StatusError struct {
	Code int
	Body string
}

func (e *StatusError) Error() string {
	return fmt.Sprintf("kubernetes API answered %d: %s", e.Code, e.Body)
}

// Pod is the part of a pod the proxy reads.
type Pod struct {
	Metadata struct {
		Name              string            `json:"name"`
		Namespace         string            `json:"namespace"`
		UID               string            `json:"uid"`
		ResourceVersion   string            `json:"resourceVersion"`
		Labels            map[string]string `json:"labels"`
		DeletionTimestamp *time.Time        `json:"deletionTimestamp"`
	} `json:"metadata"`
	Spec struct {
		InitContainers []Container `json:"initContainers"`
		Containers     []Container `json:"containers"`
	} `json:"spec"`
	Status struct {
		Phase  string `json:"phase"`
		PodIP  string `json:"podIP"`
		PodIPs []struct {
			IP string `json:"ip"`
		} `json:"podIPs"`
		InitContainerStatuses []ContainerStatus `json:"initContainerStatuses"`
		ContainerStatuses     []ContainerStatus `json:"containerStatuses"`
	} `json:"status"`
}

// Container is a container of the pod spec.
type Container struct {
	Name          string `json:"name"`
	RestartPolicy string `json:"restartPolicy"`
}

// ContainerStatus is the kubelet's view of one container.
type ContainerStatus struct {
	Name  string `json:"name"`
	State struct {
		Running    *struct{} `json:"running"`
		Terminated *struct {
			ExitCode int32 `json:"exitCode"`
		} `json:"terminated"`
	} `json:"state"`
}

// IPs are the pod's addresses.
func (p *Pod) IPs() []string {
	var ips []string
	for _, ip := range p.Status.PodIPs {
		if ip.IP != "" {
			ips = append(ips, ip.IP)
		}
	}
	if len(ips) == 0 && p.Status.PodIP != "" {
		ips = append(ips, p.Status.PodIP)
	}
	return ips
}

// Ended reports whether the pod's containers are gone (its address may be reused).
func (p *Pod) Ended() bool { return p.Status.Phase == "Succeeded" || p.Status.Phase == "Failed" }

// EventType is a watch event's type.
type EventType string

const (
	Added    EventType = "ADDED"
	Modified EventType = "MODIFIED"
	Deleted  EventType = "DELETED"
)

// Handler receives every pod of a watched namespace as it is listed or changes. A relist
// calls Sync with the complete set first.
type Handler interface {
	Sync(namespace string, pods []*Pod)
	Event(t EventType, pod *Pod)
}

// WatchPods lists and watches the pods of one namespace with a label selector until ctx
// ends, relisting after an expired resource version and backing off after errors.
func (c *Client) WatchPods(ctx context.Context, namespace, selector string, h Handler, log *slog.Logger) {
	backoff := time.Second
	for ctx.Err() == nil {
		version, err := c.list(ctx, namespace, selector, h)
		if err == nil {
			backoff = time.Second
			err = c.watch(ctx, namespace, selector, version, h)
		}
		if ctx.Err() != nil {
			return
		}
		if err != nil && !errors.Is(err, errExpired) {
			log.Warn("pod watch failed, retrying", "namespace", namespace, "error", err, "in", backoff)
			select {
			case <-ctx.Done():
				return
			case <-time.After(backoff):
			}
			backoff = min(backoff*2, 30*time.Second)
		}
	}
}

var errExpired = errors.New("resource version expired")

func (c *Client) list(ctx context.Context, namespace, selector string, h Handler) (string, error) {
	resp, err := c.get(ctx, "/api/v1/namespaces/"+namespace+"/pods", url.Values{"labelSelector": {selector}})
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	var list struct {
		Metadata struct {
			ResourceVersion string `json:"resourceVersion"`
		} `json:"metadata"`
		Items []*Pod `json:"items"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&list); err != nil {
		return "", err
	}
	h.Sync(namespace, list.Items)
	return list.Metadata.ResourceVersion, nil
}

func (c *Client) watch(ctx context.Context, namespace, selector, version string, h Handler) error {
	for ctx.Err() == nil {
		query := url.Values{
			"labelSelector":       {selector},
			"watch":               {"1"},
			"resourceVersion":     {version},
			"allowWatchBookmarks": {"true"},
			"timeoutSeconds":      {"300"},
		}
		resp, err := c.get(ctx, "/api/v1/namespaces/"+namespace+"/pods", query)
		if err != nil {
			var status *StatusError
			if errors.As(err, &status) && status.Code == http.StatusGone {
				return errExpired
			}
			return err
		}
		version, err = readEvents(resp.Body, version, h)
		resp.Body.Close()
		if err != nil {
			return err
		}
	}
	return ctx.Err()
}

// readEvents applies a watch stream's events and returns the last resource version seen.
func readEvents(body io.Reader, version string, h Handler) (string, error) {
	reader := bufio.NewReaderSize(body, 1<<16)
	decoder := json.NewDecoder(reader)
	for {
		var event struct {
			Type   string          `json:"type"`
			Object json.RawMessage `json:"object"`
		}
		if err := decoder.Decode(&event); err != nil {
			if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
				return version, nil
			}
			return version, err
		}
		switch event.Type {
		case "ERROR":
			var status struct {
				Code int `json:"code"`
			}
			_ = json.Unmarshal(event.Object, &status)
			if status.Code == http.StatusGone {
				return version, errExpired
			}
			return version, fmt.Errorf("watch error: %s", string(event.Object))
		case "BOOKMARK":
			var pod Pod
			if err := json.Unmarshal(event.Object, &pod); err == nil && pod.Metadata.ResourceVersion != "" {
				version = pod.Metadata.ResourceVersion
			}
		case string(Added), string(Modified), string(Deleted):
			var pod Pod
			if err := json.Unmarshal(event.Object, &pod); err != nil {
				return version, err
			}
			if pod.Metadata.ResourceVersion != "" {
				version = pod.Metadata.ResourceVersion
			}
			h.Event(EventType(event.Type), &pod)
		}
	}
}
