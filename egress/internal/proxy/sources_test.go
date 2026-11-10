package proxy

import (
	"context"
	"encoding/json"
	"fmt"
	"testing"
	"time"

	"github.com/musabeytekin/exradius/egress/internal/kube"
)

func pod(t *testing.T, raw string) *kube.Pod {
	t.Helper()
	var p kube.Pod
	if err := json.Unmarshal([]byte(raw), &p); err != nil {
		t.Fatal(err)
	}
	return &p
}

const taskPod = `{
  "metadata": {"name": "run-1-0", "namespace": "exradius-runs", "uid": "u1",
               "labels": {"exradius.dev/run": "run-1", "exradius.dev/purpose": "task", "exradius.dev/project": "bank"}},
  "spec": {
    "initContainers": [{"name": "prepare"}, {"name": "clone-0"}, {"name": "service-db", "restartPolicy": "Always"},
                       {"name": "setup-0"}, {"name": "files", "restartPolicy": "Always"}],
    "containers": [{"name": "workstation"}]},
  "status": {"phase": "Pending", "podIP": "10.244.0.9", "podIPs": [{"ip": "10.244.0.9"}],
    "initContainerStatuses": %s,
    "containerStatuses": %s}
}`

func taskPodWith(t *testing.T, inits, mains string) *kube.Pod {
	return pod(t, fmt.Sprintf(taskPod, inits, mains))
}

func TestPhaseOf(t *testing.T) {
	terminated := `{"terminated": {"exitCode": 0}}`
	running := `{"running": {}}`
	cases := []struct {
		name         string
		inits, mains string
		want         Phase
	}{
		{"nothing started", `[]`, `[]`, PhaseSetup},
		{"cloning", `[{"name": "prepare", "state": ` + terminated + `}, {"name": "clone-0", "state": ` + running + `}]`, `[]`, PhaseSetup},
		{"service up, setup running",
			`[{"name": "prepare", "state": ` + terminated + `}, {"name": "clone-0", "state": ` + terminated + `},
			  {"name": "service-db", "state": ` + running + `}, {"name": "setup-0", "state": ` + running + `}]`, `[]`, PhaseSetup},
		{"setup done, workstation not reported yet",
			`[{"name": "prepare", "state": ` + terminated + `}, {"name": "clone-0", "state": ` + terminated + `},
			  {"name": "service-db", "state": ` + running + `}, {"name": "setup-0", "state": ` + terminated + `}]`, `[]`, PhaseAgent},
		{"workstation running", `[]`, `[{"name": "workstation", "state": ` + running + `}]`, PhaseAgent},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := PhaseOf(taskPodWith(t, c.inits, c.mains)); got != c.want {
				t.Fatalf("PhaseOf = %s, want %s", got, c.want)
			}
		})
	}

	probe := pod(t, `{"metadata": {"labels": {"exradius.dev/run": "probe-1", "exradius.dev/purpose": "probe"}},
		"spec": {"containers": [{"name": "workstation"}]}, "status": {}}`)
	if got := PhaseOf(probe); got != PhaseAgent {
		t.Fatalf("a probe pod (no init containers) is in the agent phase from the start, got %s", got)
	}
	build := pod(t, `{"metadata": {"labels": {"exradius.dev/purpose": "build"}},
		"spec": {"initContainers": [{"name": "context"}], "containers": [{"name": "build"}]}, "status": {}}`)
	if got := PhaseOf(build); got != PhaseBuild {
		t.Fatalf("build pod phase %s", got)
	}
}

func TestSourcesFollowThePodWatch(t *testing.T) {
	s := NewSources("exradius-runs")
	if s.Ready() {
		t.Fatal("ready before the first list")
	}
	setup := taskPodWith(t, `[]`, `[]`)
	s.Sync("exradius-runs", []*kube.Pod{setup})
	if !s.Ready() {
		t.Fatal("not ready after the list")
	}
	ctx := context.Background()
	src := s.Lookup(ctx, "10.244.0.9", 0)
	if src == nil || src.Run != "run-1" || src.Phase != PhaseSetup || src.Project != "bank" {
		t.Fatalf("lookup after sync: %+v", src)
	}

	agent := taskPodWith(t, `[]`, `[{"name": "workstation", "state": {"running": {}}}]`)
	s.Event(kube.Modified, agent)
	if src := s.Lookup(ctx, "10.244.0.9", 0); src.Phase != PhaseAgent {
		t.Fatalf("phase after the workstation started: %s", src.Phase)
	}
	// A later status that looks like setup again (a stale event) does not go back.
	s.Event(kube.Modified, setup)
	if src := s.Lookup(ctx, "10.244.0.9", 0); src.Phase != PhaseAgent {
		t.Fatalf("the phase went back to %s", src.Phase)
	}

	ended := taskPodWith(t, `[]`, `[{"name": "workstation", "state": {"terminated": {"exitCode": 0}}}]`)
	ended.Status.Phase = "Succeeded"
	s.Event(kube.Modified, ended)
	if src := s.Lookup(ctx, "10.244.0.9", 0); src != nil {
		t.Fatalf("an ended pod's address still maps to %+v", src)
	}

	s.Event(kube.Added, setup)
	s.Event(kube.Deleted, setup)
	if src := s.Lookup(ctx, "10.244.0.9", 0); src != nil {
		t.Fatal("a deleted pod is still known")
	}
}

func TestSyncForgetsPodsGoneFromTheList(t *testing.T) {
	s := NewSources("exradius-runs")
	s.Sync("exradius-runs", []*kube.Pod{taskPodWith(t, `[]`, `[]`)})
	s.Sync("exradius-runs", nil)
	if src := s.Lookup(context.Background(), "10.244.0.9", 0); src != nil {
		t.Fatal("a pod missing from a relist is still known")
	}
}

func TestLookupWaitsForAPodThatJustStarted(t *testing.T) {
	s := NewSources("exradius-runs")
	go func() {
		time.Sleep(30 * time.Millisecond)
		s.Event(kube.Added, taskPodWith(t, `[]`, `[]`))
	}()
	if src := s.Lookup(context.Background(), "10.244.0.9", 2*time.Second); src == nil {
		t.Fatal("the pod was not found once the watch reported it")
	}
	if src := s.Lookup(context.Background(), "10.244.0.77", 20*time.Millisecond); src != nil {
		t.Fatal("an unknown address was found")
	}
}

func TestPodsWithoutARunLabelAreIgnored(t *testing.T) {
	s := NewSources("exradius-runs")
	stray := pod(t, `{"metadata": {"uid": "x", "labels": {}}, "status": {"podIP": "10.244.0.50"}}`)
	s.Event(kube.Added, stray)
	if src := s.Lookup(context.Background(), "10.244.0.50", 0); src != nil {
		t.Fatal("a pod without exradius.dev/run became a source")
	}
}
