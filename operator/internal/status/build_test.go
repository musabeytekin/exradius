package status

import (
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	v1 "github.com/musabeytekin/exradius/operator/api/v1alpha1"
	"github.com/musabeytekin/exradius/operator/internal/podspec"
)

const metadata = `{
  "containerimage.config.digest": "sha256:2cb3",
  "containerimage.digest": "sha256:4118",
  "image.name": "kind-registry:5000/exradius/saleor/workstation:1a2b"
}`

func build() *v1.Build {
	return &v1.Build{
		ObjectMeta: metav1.ObjectMeta{Name: "build-saleor-workstation-1a2b", Namespace: "exradius-builds", Generation: 1},
		Spec: v1.BuildSpec{ID: "b1", Project: "saleor", Name: "workstation", Image: "kind-registry:5000/exradius/saleor/workstation:1a2b",
			Dockerfile: "Dockerfile", SecretName: "build-saleor-workstation-1a2b"},
		Status: v1.BuildStatus{Phase: v1.PhasePending, PodName: "build-saleor-workstation-1a2b"},
	}
}

func buildPod(phase corev1.PodPhase, node string, inits, mains []corev1.ContainerStatus) *corev1.Pod {
	p := pod(phase, node, inits, mains)
	p.Name = "build-saleor-workstation-1a2b"
	if node != "" {
		p.Status.StartTime = &metav1.Time{Time: now}
	}
	return p
}

func TestBuildDigest(t *testing.T) {
	if got := BuildDigest(metadata); got != "sha256:4118" {
		t.Fatalf("digest %q", got)
	}
	if BuildDigest("") != "" || BuildDigest("not json") != "" {
		t.Fatal("no metadata, no digest")
	}
}

func TestBuildPending(t *testing.T) {
	p := buildPod(corev1.PodPending, "", nil, nil)
	p.Status.Conditions = []corev1.PodCondition{{Type: corev1.PodScheduled, Status: corev1.ConditionFalse,
		Reason: "Unschedulable", Message: "0/1 nodes are available: 1 Insufficient memory."}}
	obs := ObserveBuild(build(), p, now)
	if obs.Status.Phase != v1.PhasePending || obs.Status.Reason != "Unschedulable" || obs.Status.StartedAt != nil {
		t.Fatalf("status %+v", obs.Status)
	}
	if obs.LogFrom != "" {
		t.Fatal("no log before the build starts")
	}
}

func TestBuildRunning(t *testing.T) {
	p := buildPod(corev1.PodPending, "node", []corev1.ContainerStatus{running(podspec.ContainerContext)}, nil)
	obs := ObserveBuild(build(), p, now)
	if obs.Status.Phase != v1.PhaseRunning || obs.Status.StartedAt == nil || obs.LogFrom != "" {
		t.Fatalf("fetching the context: %+v, log from %q", obs.Status, obs.LogFrom)
	}
	p = buildPod(corev1.PodRunning, "node", []corev1.ContainerStatus{terminated(podspec.ContainerContext, 0, "Completed")},
		[]corev1.ContainerStatus{running(podspec.ContainerBuild)})
	obs = ObserveBuild(build(), p, now)
	if obs.Status.Phase != v1.PhaseRunning || obs.LogFrom != podspec.ContainerBuild {
		t.Fatalf("building: %+v, log from %q", obs.Status, obs.LogFrom)
	}
}

func TestBuildSucceeded(t *testing.T) {
	done := terminated(podspec.ContainerBuild, 0, "Completed")
	done.State.Terminated.Message = metadata
	p := buildPod(corev1.PodSucceeded, "node", []corev1.ContainerStatus{terminated(podspec.ContainerContext, 0, "Completed")},
		[]corev1.ContainerStatus{done})
	obs := ObserveBuild(build(), p, now)
	st := obs.Status
	if st.Phase != v1.PhaseSucceeded || st.Digest != "sha256:4118" || st.FinishedAt == nil || obs.LogFrom != podspec.ContainerBuild {
		t.Fatalf("status %+v", st)
	}
	if len(obs.Events) != 1 || obs.Events[0].Reason != "Built" || !strings.Contains(obs.Events[0].Note, "@sha256:4118") {
		t.Fatalf("events %+v", obs.Events)
	}
	again := build()
	again.Status = st
	if events := ObserveBuild(again, p, now).Events; len(events) != 0 {
		t.Fatalf("an outcome is announced once, got %+v", events)
	}
}

func TestBuildFailed(t *testing.T) {
	p := buildPod(corev1.PodFailed, "node", []corev1.ContainerStatus{terminated(podspec.ContainerContext, 0, "Completed")},
		[]corev1.ContainerStatus{terminated(podspec.ContainerBuild, 1, "Error")})
	obs := ObserveBuild(build(), p, now)
	if obs.Status.Phase != v1.PhaseFailed || obs.Status.Reason != v1.ReasonBuildFailed || obs.Status.Message != "the build exited with 1" {
		t.Fatalf("status %+v", obs.Status)
	}
	if obs.LogFrom != podspec.ContainerBuild || obs.Fail {
		t.Fatal("the build's log; the pod failed on its own")
	}

	p = buildPod(corev1.PodFailed, "node", nil, []corev1.ContainerStatus{terminated(podspec.ContainerBuild, 137, "OOMKilled")})
	if obs = ObserveBuild(build(), p, now); obs.Status.Reason != v1.ReasonOOMKilled {
		t.Fatalf("OOM: %+v", obs.Status)
	}
}

func TestBuildContextFailed(t *testing.T) {
	p := buildPod(corev1.PodFailed, "node", []corev1.ContainerStatus{terminated(podspec.ContainerContext, 1, "Error")},
		[]corev1.ContainerStatus{waiting(podspec.ContainerBuild, "PodInitializing", "")})
	obs := ObserveBuild(build(), p, now)
	if obs.Status.Phase != v1.PhaseFailed || obs.Status.Reason != v1.ReasonContextFailed || obs.LogFrom != podspec.ContainerContext {
		t.Fatalf("status %+v, log from %q", obs.Status, obs.LogFrom)
	}
}

func TestBuilderImagePullFails(t *testing.T) {
	b := build()
	for attempt := 1; attempt <= PullAttempts; attempt++ {
		for _, reason := range []string{"ErrImagePull", "ImagePullBackOff"} {
			p := buildPod(corev1.PodPending, "node", []corev1.ContainerStatus{waiting(podspec.ContainerContext, reason, "not found")}, nil)
			obs := ObserveBuild(b, p, now)
			b.Status = obs.Status
			if attempt == PullAttempts && reason == "ErrImagePull" {
				if !obs.Fail || obs.Status.Reason != v1.ReasonImagePullFailed {
					t.Fatalf("attempt %d: %+v", attempt, obs.Status)
				}
				return
			}
			if obs.Fail {
				t.Fatalf("failed too early at attempt %d (%s)", attempt, reason)
			}
		}
	}
}
