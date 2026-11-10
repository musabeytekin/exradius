// Package proxy is the egress proxy (docs/kubernetes.md §11): a forward proxy that decides
// by host name on CONNECT, an injecting reverse proxy for a run's model, repositories and
// HTTP connections, and the audit of every request. A request's run is its source
// address: the pod watch maps pod IPs to Runs and tells the setup phase from the agent's.
package proxy

import (
	"context"
	"sync"
	"time"

	"github.com/musabeytekin/exradius/egress/internal/kube"
)

// Phase is what a pod is doing, which decides what it may reach.
type Phase string

const (
	// PhaseSetup is a run pod before its last setup command ended: prepare, the clones,
	// the services and the setup commands. Any host.
	PhaseSetup Phase = "setup"
	// PhaseAgent is a run pod from its workstation's start: the allowlist only.
	PhaseAgent Phase = "agent"
	// PhaseBuild is a build pod: any host.
	PhaseBuild Phase = "build"
)

// Open reports whether the phase may reach any host.
func (p Phase) Open() bool { return p == PhaseSetup || p == PhaseBuild }

// Labels the operator puts on run and build pods.
const (
	LabelRun     = "exradius.dev/run"
	LabelPurpose = "exradius.dev/purpose"
	LabelProject = "exradius.dev/project"
	PurposeBuild = "build"
)

// Source is the pod a connection comes from.
type Source struct {
	Namespace string
	Pod       string
	UID       string
	// Run is the Run's name (run-<id>, verify-<id>, probe-<id>); empty for build pods.
	Run     string
	Purpose string
	Project string
	Phase   Phase
	IPs     []string
}

// Build reports whether the source is a build pod.
func (s *Source) Build() bool { return s.Purpose == PurposeBuild }

// PhaseOf is a pod's phase from its spec and status. A run pod is in the agent phase once
// its main container (the workstation) has started, or once every init container that is
// not a sidecar (prepare, clone-*, setup-*) has terminated; before that it is in setup.
func PhaseOf(pod *kube.Pod) Phase {
	if pod.Metadata.Labels[LabelPurpose] == PurposeBuild {
		return PhaseBuild
	}
	for _, status := range pod.Status.ContainerStatuses {
		if status.State.Running != nil || status.State.Terminated != nil {
			return PhaseAgent
		}
	}
	terminated := map[string]bool{}
	for _, status := range pod.Status.InitContainerStatuses {
		if status.State.Terminated != nil {
			terminated[status.Name] = true
		}
	}
	for _, c := range pod.Spec.InitContainers {
		if c.RestartPolicy == "Always" {
			continue // a sidecar: a service or the files sidecar
		}
		if !terminated[c.Name] {
			return PhaseSetup
		}
	}
	return PhaseAgent
}

// Sources maps pod addresses to their pods, from the pod watch. A pod's phase only moves
// forward: once in the agent phase it stays there.
type Sources struct {
	mu      sync.Mutex
	byIP    map[string]*Source
	byUID   map[string]*Source
	changed chan struct{}
	synced  map[string]bool
}

// NewSources is an empty map; it is ready once every namespace in want has been listed.
func NewSources(want ...string) *Sources {
	s := &Sources{byIP: map[string]*Source{}, byUID: map[string]*Source{}, changed: make(chan struct{}), synced: map[string]bool{}}
	for _, ns := range want {
		s.synced[ns] = false
	}
	return s
}

// Ready reports whether every watched namespace has been listed once.
func (s *Sources) Ready() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, ok := range s.synced {
		if !ok {
			return false
		}
	}
	return true
}

func (s *Sources) notify() {
	close(s.changed)
	s.changed = make(chan struct{})
}

func sourceOf(pod *kube.Pod) *Source {
	labels := pod.Metadata.Labels
	return &Source{
		Namespace: pod.Metadata.Namespace,
		Pod:       pod.Metadata.Name,
		UID:       pod.Metadata.UID,
		Run:       labels[LabelRun],
		Purpose:   labels[LabelPurpose],
		Project:   labels[LabelProject],
		Phase:     PhaseOf(pod),
		IPs:       pod.IPs(),
	}
}

// put records a pod (s.mu held).
func (s *Sources) put(pod *kube.Pod) {
	s.drop(pod.Metadata.UID)
	if pod.Ended() {
		return // its address may already belong to another pod
	}
	src := sourceOf(pod)
	if src.Run == "" && !src.Build() {
		return
	}
	if old := s.byUID[src.UID]; old != nil && old.Phase == PhaseAgent {
		src.Phase = PhaseAgent
	}
	s.byUID[src.UID] = src
	for _, ip := range src.IPs {
		s.byIP[ip] = src
	}
}

// drop forgets a pod (s.mu held), keeping its last phase for a pod that comes back.
func (s *Sources) drop(uid string) {
	old := s.byUID[uid]
	if old == nil {
		return
	}
	for _, ip := range old.IPs {
		if s.byIP[ip] == old {
			delete(s.byIP, ip)
		}
	}
}

// Sync implements kube.Handler: the namespace's complete set of pods.
func (s *Sources) Sync(namespace string, pods []*kube.Pod) {
	s.mu.Lock()
	defer s.mu.Unlock()
	keep := map[string]bool{}
	for _, pod := range pods {
		keep[pod.Metadata.UID] = true
		s.put(pod)
	}
	for uid, src := range s.byUID {
		if src.Namespace == namespace && !keep[uid] {
			s.drop(uid)
			delete(s.byUID, uid)
		}
	}
	s.synced[namespace] = true
	s.notify()
}

// Event implements kube.Handler.
func (s *Sources) Event(t kube.EventType, pod *kube.Pod) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if t == kube.Deleted {
		s.drop(pod.Metadata.UID)
		delete(s.byUID, pod.Metadata.UID)
	} else {
		s.put(pod)
	}
	s.notify()
}

// Lookup is the pod at ip. A pod that has just started may not be known yet, so it waits
// up to wait for the watch to report it.
func (s *Sources) Lookup(ctx context.Context, ip string, wait time.Duration) *Source {
	deadline := time.NewTimer(wait)
	defer deadline.Stop()
	for {
		s.mu.Lock()
		src, changed := s.byIP[ip], s.changed
		s.mu.Unlock()
		if src != nil {
			copied := *src
			return &copied
		}
		select {
		case <-changed:
		case <-deadline.C:
			return nil
		case <-ctx.Done():
			return nil
		}
	}
}

// Set records a source directly (tests, and a proxy without a pod watch).
func (s *Sources) Set(src *Source) {
	s.mu.Lock()
	defer s.mu.Unlock()
	copied := *src
	s.byUID[src.UID] = &copied
	for _, ip := range src.IPs {
		s.byIP[ip] = &copied
	}
	s.notify()
}
